"""The AST guard — layer 2 of the defense-in-depth stack.

Parses generated SQL with pglast (bindings to libpg_query, the actual
Postgres parser — not a regex, not a keyword blocklist) and walks the real
parse tree. What this module approves is exactly the tree the server will
execute: there is no lexical trick that separates the two, which is why
`SEL/**/ECT`, `WITH d AS (DELETE ... RETURNING *) SELECT * FROM d`, and
`SELECT pg_sleep(9999)` all get caught here rather than surviving to
production. See the "Bypass" table in the project plan.

Design: allowlist the statement shape (single bare SELECT), denylist a
short list of functions/schemas that a plain SELECT could still reach.
The allowlist does the heavy lifting — PREPARE, EXECUTE, CALL, COPY,
VACUUM, LOCK, SET, TRUNCATE, GRANT, DO, and every DDL form are rejected for
free because none of them parse as a SelectStmt. The denylist exists only
for capabilities reachable *from inside* a SelectStmt (function calls,
schema-qualified reads, row locks).

The approved SQL that reaches the database is re-deparsed from the AST
this module just checked, not the caller's original string — no gap
between "what was verified" and "what runs".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from pglast import ast, enums, parse_sql
from pglast.error import Error as PglastError
from pglast.stream import RawStream
from pglast.visitors import Visitor

from .errors import TERMINAL_REASONS, RejectReason

# ---------------------------------------------------------------------------
# Denylists — narrow and specific on purpose. Every entry earns its place
# by being a capability that survives inside an otherwise-ordinary SELECT.
# ---------------------------------------------------------------------------

#: Functions with a side effect, a resource-exhaustion risk, or a direct
#: filesystem/network reach. Checked by name regardless of where the call
#: appears (target list, WHERE clause, or FROM-clause table function) —
#: `SELECT * FROM pg_read_file(...)` is caught the same way as
#: `SELECT pg_read_file(...)`.
DENIED_FUNCTIONS: frozenset[str] = frozenset(
    {
        # Timing / resource abuse
        "pg_sleep", "pg_sleep_for", "pg_sleep_until",
        "pg_advisory_lock", "pg_advisory_lock_shared",
        "pg_advisory_xact_lock", "pg_advisory_xact_lock_shared",
        # Filesystem / large object access
        "pg_read_file", "pg_read_binary_file", "pg_stat_file",
        "pg_ls_dir", "pg_ls_logdir", "pg_ls_waldir", "pg_ls_archive_statusdir",
        "lo_import", "lo_export", "lo_get", "lo_put", "lo_read", "lo_write",
        # Cross-database / network reach
        "dblink", "dblink_connect", "dblink_connect_u", "dblink_exec",
        "dblink_open",
        # Session / server tampering
        "set_config", "pg_terminate_backend", "pg_cancel_backend",
        "pg_reload_conf", "pg_rotate_logfile",
        "pg_read_server_files", "pg_write_server_files", "pg_execute_server_program",
    }
)

#: Schemas holding system catalogs and internals. A read-only SELECT against
#: these can enumerate roles, passwords hashes (pg_authid), and server
#: internals that have nothing to do with the analytics warehouse.
DENIED_SCHEMAS: frozenset[str] = frozenset({"pg_catalog", "information_schema", "pg_toast"})

#: Well-known system relations, denied even when referenced unqualified
#: (the connecting role's search_path is pinned to `analytics` at the
#: database level — see db/02_roles.sql — but the guard doesn't rely on
#: that alone).
DENIED_UNQUALIFIED_RELATIONS: frozenset[str] = frozenset(
    {
        "pg_shadow", "pg_authid", "pg_user", "pg_roles",
        "pg_settings", "pg_stat_activity", "pg_hba_file_rules",
    }
)

#: The only statement shape this pipeline ever generates.
ALLOWED_TOP_LEVEL: frozenset[type] = frozenset({ast.SelectStmt})

#: Statement types that must never appear anywhere in the tree — including
#: nested inside a CTE, where the *outer* statement is still a harmless
#: SelectStmt (`WITH d AS (DELETE FROM x RETURNING *) SELECT * FROM d`).
#: The top-level check alone does not catch this; the tree walk does.
DENIED_STMT_TYPES: tuple[type, ...] = (
    ast.InsertStmt, ast.UpdateStmt, ast.DeleteStmt, ast.MergeStmt,
    ast.TruncateStmt, ast.DropStmt, ast.CreateStmt, ast.AlterTableStmt,
    ast.CreateTableAsStmt, ast.DoStmt, ast.CallStmt, ast.CopyStmt,
    ast.GrantStmt, ast.GrantRoleStmt, ast.VacuumStmt, ast.LockStmt,
    ast.VariableSetStmt, ast.PrepareStmt, ast.ExecuteStmt,
)

DEFAULT_ROW_CAP = 1000


class Catalog(Protocol):
    """What the guard needs from the schema layer (Phase 02) to catch
    hallucinated identifiers. Optional — pass ``None`` to skip these checks
    entirely, which is the right choice before a catalog snapshot exists.
    """

    def has_relation(self, schema: str | None, name: str) -> bool: ...
    def nearest_relations(self, name: str, limit: int = 3) -> list[str]: ...


@dataclass
class Violation:
    reason: RejectReason
    detail: str
    node_repr: str = ""


@dataclass
class GuardResult:
    ok: bool
    safe_sql: str | None
    reason: RejectReason | None
    detail: str
    terminal: bool = False
    violations: list[Violation] = field(default_factory=list)


# Ordering used only to pick a single headline reason when several
# violations exist in one statement — the full list still ships in
# `violations` / `detail` for logging.
_SEVERITY = (
    RejectReason.DISALLOWED_STATEMENT_TYPE,
    RejectReason.UNSAFE_FUNCTION,
    RejectReason.CATALOG_SCHEMA_ACCESS,
    RejectReason.LOCKING_CLAUSE,
    RejectReason.UNKNOWN_TABLE,
    RejectReason.UNKNOWN_COLUMN,
)


def _func_name(node: ast.FuncCall) -> str:
    # funcname is a tuple of String nodes; the last is the bare function
    # name (`pg_catalog.pg_sleep` -> "pg_sleep") — schema-qualifying a
    # denied function doesn't help an attacker.
    return node.funcname[-1].sval.lower()


class _SemanticWalker(Visitor):
    """Walks an already-approved single-SELECT tree looking for the
    capabilities in the denylists above. Recurses into subqueries, CTEs,
    and table functions automatically (that's what makes it catch a
    LockingClause or FuncCall no matter how deeply nested it is).
    """

    def __init__(self, catalog: Catalog | None, local_names: frozenset[str] = frozenset()) -> None:
        super().__init__()
        self.catalog = catalog
        self.violations: list[Violation] = []
        # Names defined by the query's own WITH clause(s) — a reference to
        # one is a CTE reference, not a table that has to exist in the
        # catalog. Collected once, up front (see _collect_cte_names),
        # rather than tracked scope-by-scope during the walk: CTE name
        # shadowing across nesting levels is a real SQL feature but not a
        # security-relevant distinction here, and erring toward "known
        # locally" only ever makes the guard more permissive of legitimate
        # queries, never less safe — the denylists above still apply
        # regardless of what a RangeVar's name happens to match.
        self.local_names = local_names

    def visit_SelectStmt(self, ancestors, node: ast.SelectStmt):
        if node.intoClause is not None:
            self.violations.append(
                Violation(
                    RejectReason.DISALLOWED_STATEMENT_TYPE,
                    "SELECT INTO creates a table; only a plain SELECT is allowed",
                )
            )
        if node.lockingClause:
            self.violations.append(
                Violation(
                    RejectReason.LOCKING_CLAUSE,
                    "FOR UPDATE / FOR SHARE takes row locks; not permitted on a read-only pipeline",
                )
            )
        # Keep walking into the rest of this node (target list, FROM, WHERE,
        # subqueries) — returning None (not visitors.Skip) continues normally.

    def visit_FuncCall(self, ancestors, node: ast.FuncCall):
        name = _func_name(node)
        if name in DENIED_FUNCTIONS:
            self.violations.append(
                Violation(RejectReason.UNSAFE_FUNCTION, f"function '{name}' is not permitted", name)
            )

    def visit_RangeVar(self, ancestors, node: ast.RangeVar):
        schema = node.schemaname
        if schema is not None and schema in DENIED_SCHEMAS:
            self.violations.append(
                Violation(
                    RejectReason.CATALOG_SCHEMA_ACCESS,
                    f"schema '{schema}' is not queryable from this pipeline",
                    f"{schema}.{node.relname}",
                )
            )
        elif schema is None and node.relname in DENIED_UNQUALIFIED_RELATIONS:
            self.violations.append(
                Violation(
                    RejectReason.CATALOG_SCHEMA_ACCESS,
                    f"relation '{node.relname}' is a system catalog, not an analytics table",
                    node.relname,
                )
            )
        elif schema is None and node.relname in self.local_names:
            pass  # a reference to a CTE defined in this same query, not a real table
        elif self.catalog is not None and (schema is None or schema == "analytics"):
            # Generated SQL may or may not schema-qualify — chatbot_ro's
            # search_path is pinned to analytics (db/02_roles.sql), so both
            # `v_orders` and `analytics.v_orders` are the same relation and
            # both deserve a catalog check. Anything qualified with some
            # OTHER schema isn't this branch's concern (see SnapshotCatalog
            # .has_relation): it's either already caught by the schema
            # denylist above, or will fail at execution for lack of grants.
            if not self.catalog.has_relation(schema, node.relname):
                suggestions = self.catalog.nearest_relations(node.relname)
                hint = f" — did you mean: {', '.join(suggestions)}?" if suggestions else ""
                self.violations.append(
                    Violation(
                        RejectReason.UNKNOWN_TABLE,
                        f"relation '{node.relname}' does not exist{hint}",
                        node.relname,
                    )
                )


def _headline(violations: list[Violation]) -> Violation:
    by_reason = {v.reason: v for v in reversed(violations)}
    for reason in _SEVERITY:
        if reason in by_reason:
            return by_reason[reason]
    return violations[0]


def _inject_row_cap(stmt: ast.SelectStmt, cap: int) -> None:
    """Add a LIMIT only if the statement doesn't already have one. This is
    a courtesy, not the hard backstop — the execution envelope (Phase 04)
    enforces the real cap via cursor-paged fetch regardless of what LIMIT
    the SQL claims, so a query that lies about its own limit still can't
    flood the caller.
    """
    if stmt.limitCount is None:
        stmt.limitCount = ast.A_Const(val=ast.Integer(ival=cap))
        stmt.limitOption = enums.LimitOption.LIMIT_OPTION_COUNT


def _collect_cte_names(raw_stmt: ast.RawStmt) -> frozenset[str]:
    """Every CommonTableExpr name anywhere in the tree, regardless of
    nesting depth — see _SemanticWalker.local_names for why a single
    flat collection (not scope-aware) is the right amount of precision
    here. Found necessary by testing: the golden-set eval harness (Phase
    05) surfaced that `WITH order_totals AS (...) SELECT ... FROM
    order_totals` was being rejected as UNKNOWN_TABLE — a real, would-
    have-shipped bug that predates this fix and would have blocked any
    correctly-formed multi-CTE query from any model, not just this
    project's.
    """
    names: list[str] = []

    class _CteFinder(Visitor):
        def visit_CommonTableExpr(self, ancestors, node: ast.CommonTableExpr):
            names.append(node.ctename)

    _CteFinder()(raw_stmt)
    return frozenset(names)


def check(sql: str, catalog: Catalog | None = None, row_cap: int = DEFAULT_ROW_CAP) -> GuardResult:
    """Validate a generated SQL string. Returns a GuardResult — never
    raises for anything the pipeline should treat as an ordinary rejection
    (syntax errors and safety violations alike come back as `ok=False`
    with a typed reason, not an exception).
    """
    if not sql or not sql.strip():
        return GuardResult(False, None, RejectReason.EMPTY_STATEMENT, "empty statement")

    try:
        tree = parse_sql(sql)
    except PglastError as exc:
        return GuardResult(False, None, RejectReason.SYNTAX_ERROR, str(exc))

    if len(tree) == 0:
        return GuardResult(False, None, RejectReason.EMPTY_STATEMENT, "empty statement")
    if len(tree) > 1:
        return GuardResult(
            False, None, RejectReason.MULTI_STATEMENT,
            f"{len(tree)} statements in one request; only a single SELECT is allowed",
            terminal=True,
        )

    raw_stmt = tree[0]
    stmt = raw_stmt.stmt
    if type(stmt) not in ALLOWED_TOP_LEVEL:
        return GuardResult(
            False, None, RejectReason.DISALLOWED_STATEMENT_TYPE,
            f"statement type '{type(stmt).__name__}' is not permitted; only SELECT is",
            terminal=True,
        )

    walker = _SemanticWalker(catalog, local_names=_collect_cte_names(raw_stmt))
    walker(raw_stmt)

    # Defense in depth: even though the top level is a SelectStmt, a CTE
    # body can itself be a DeleteStmt/InsertStmt/etc. Visitor recurses into
    # CTE bodies automatically, so catch those here rather than trusting
    # the top-level check alone.
    nested_denied = _find_nested_denied_statement(raw_stmt)
    if nested_denied is not None:
        return GuardResult(
            False, None, RejectReason.DISALLOWED_STATEMENT_TYPE,
            f"'{nested_denied}' is not permitted, including nested inside a CTE",
            terminal=True,
        )

    if walker.violations:
        headline = _headline(walker.violations)
        detail = "; ".join(f"{v.reason.value}: {v.detail}" for v in walker.violations)
        return GuardResult(
            False, None, headline.reason, detail,
            terminal=headline.reason in TERMINAL_REASONS,
            violations=walker.violations,
        )

    _inject_row_cap(stmt, row_cap)
    safe_sql = RawStream()(raw_stmt)
    return GuardResult(True, safe_sql, None, "ok")


def _find_nested_denied_statement(raw_stmt: ast.RawStmt) -> str | None:
    hit: list[str] = []

    class _Finder(Visitor):
        def visit(self, ancestors, node):
            if isinstance(node, DENIED_STMT_TYPES) and node is not raw_stmt.stmt:
                hit.append(type(node).__name__)
            return None

    _Finder()(raw_stmt)
    return hit[0] if hit else None
