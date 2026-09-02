"""Zero-row selectivity diagnostic — the plan's own accounting names this
the single largest source of zero-row results, and the fix is mechanical:
re-run each of the query's WHERE-clause predicates cumulatively and report
the one that emptied the result, rather than making the user guess which
of five AND'd conditions was the culprit.

Built from the AST, not string surgery: pglast's Visitor confirmed
(during Phase 01 build/verification) that Postgres represents a chain of
AND conditions as a left-associative BoolExpr tree, so splitting it is a
straightforward walk, and every candidate diagnostic query is a real
SelectStmt node re-deparsed by the same RawStream the guard itself trusts
— never an f-string assembling clauses by hand.
"""

from __future__ import annotations

from dataclasses import dataclass

from pglast import ast, enums, parse_sql
from pglast.stream import RawStream

from ..guards import ast_guard
from .execute import execute


@dataclass(frozen=True)
class PredicateCheck:
    predicate_sql: str
    cumulative_row_count: int


@dataclass(frozen=True)
class ZeroRowDiagnosis:
    baseline_row_count: int
    checks: tuple[PredicateCheck, ...]
    culprit: str | None
    message: str


def _flatten_and(node: ast.Node) -> list[ast.Node]:
    if isinstance(node, ast.BoolExpr) and node.boolop == enums.BoolExprType.AND_EXPR:
        flattened: list[ast.Node] = []
        for arg in node.args:
            flattened.extend(_flatten_and(arg))
        return flattened
    return [node]


def _count_query_sql(from_clause: tuple, where: ast.Node | None) -> str:
    count_target = ast.ResTarget(val=ast.FuncCall(funcname=(ast.String(sval="count"),), agg_star=True))
    stmt = ast.SelectStmt(targetList=(count_target,), fromClause=from_clause, whereClause=where)
    return RawStream()(stmt)


def diagnose_zero_rows(safe_sql: str, tenant_id: int, catalog: ast_guard.Catalog | None = None) -> ZeroRowDiagnosis | None:
    """`safe_sql` must be the guard-approved query that already ran and
    returned zero rows. Returns None when there's genuinely nothing to
    diagnose (no WHERE clause at all — the source data itself is empty,
    not a filter).
    """
    tree = parse_sql(safe_sql)
    stmt = tree[0].stmt
    if stmt.whereClause is None or not stmt.fromClause:
        return None

    baseline_sql = _count_query_sql(stmt.fromClause, None)
    baseline_result = ast_guard.check(baseline_sql, catalog=catalog)
    if not baseline_result.ok:
        return None  # shouldn't happen if the original query passed the guard, but never crash a diagnostic
    baseline_count = execute(baseline_result.safe_sql, tenant_id).rows[0][0]

    conjuncts = _flatten_and(stmt.whereClause)
    checks: list[PredicateCheck] = []
    cumulative: ast.Node | None = None
    culprit: str | None = None
    prev_count = baseline_count

    for node in conjuncts:
        cumulative = node if cumulative is None else ast.BoolExpr(boolop=enums.BoolExprType.AND_EXPR, args=(cumulative, node))
        candidate_sql = _count_query_sql(stmt.fromClause, cumulative)
        candidate_result = ast_guard.check(candidate_sql, catalog=catalog)
        if not candidate_result.ok:
            continue  # skip a predicate the guard itself would reject in isolation; keep diagnosing the rest
        count = execute(candidate_result.safe_sql, tenant_id).rows[0][0]
        pred_text = RawStream()(node)
        checks.append(PredicateCheck(pred_text, count))
        if count == 0 and culprit is None and prev_count != 0:
            culprit = pred_text
        prev_count = count

    if culprit is not None:
        message = (
            f"'{culprit}' is the filter that emptied the result — "
            f"{baseline_count} row(s) before any filter, 0 after adding this one."
        )
    elif baseline_count == 0:
        message = "The underlying data itself has no matching rows before any WHERE filter is applied — not a filter mismatch."
    else:
        message = (
            f"No single filter alone explains it ({baseline_count} row(s) with no filter); "
            "the combination excludes everything even though each filter individually matches some rows."
        )

    return ZeroRowDiagnosis(baseline_row_count=baseline_count, checks=tuple(checks), culprit=culprit, message=message)
