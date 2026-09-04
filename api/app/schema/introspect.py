"""Catalog introspection. Connects with the app's own trusted credentials,
never chatbot_ro — introspecting information_schema/pg_catalog is exactly
what the AST guard denies to generated SQL.

Produces a Snapshot of exactly what chatbot_ro can query, sourced from
information_schema.role_table_grants rather than assumed from the view
list, so a view/grant mismatch is a loud SchemaDriftError instead of a
silent gap. PK/FK/comments are inferred from the base tables by name
convention (`v_<table>` -> `<table>`) since views carry none of their own.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psycopg


class SchemaDriftError(RuntimeError):
    """The view set and the chatbot_ro grant set disagree — a view with
    no grant, or a grant with no matching view."""


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str
    nullable: bool
    comment: str | None


@dataclass(frozen=True)
class ForeignKey:
    column: str
    ref_relation: str  # unqualified base table name, e.g. "orders"
    ref_column: str


@dataclass(frozen=True)
class Relation:
    name: str  # e.g. "v_orders"
    comment: str | None
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...]
    foreign_keys: tuple[ForeignKey, ...]
    row_magnitude: str
    source_note: str | None


@dataclass(frozen=True)
class Snapshot:
    schema: str
    relations: tuple[Relation, ...]
    captured_at: str
    fingerprint: str
    relation_names: frozenset[str] = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "relation_names", frozenset(r.name for r in self.relations))


def _row_magnitude(reltuples: float | None) -> str:
    # Bucketed to an order of magnitude, not the exact reltuples estimate —
    # reltuples drifts with every autovacuum, which would otherwise
    # invalidate the prompt cache on an unchanged schema.
    if reltuples is None:
        return "unknown"
    n = max(reltuples, 0)
    if n < 100:
        return "~100 rows"
    if n < 1_000:
        return "~1K rows"
    if n < 10_000:
        return "~10K rows"
    if n < 100_000:
        return "~100K rows"
    if n < 1_000_000:
        return "~1M rows"
    if n < 10_000_000:
        return "~10M rows"
    return "~10M+ rows"


_BASE_TABLE_SQL = """
    SELECT
        c.relname AS table_name,
        c.reltuples::float8 AS reltuples,
        obj_description(c.oid, 'pg_class') AS table_comment
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = %(schema)s AND c.relkind = 'r'
    ORDER BY c.relname
"""

_COLUMN_COMMENT_SQL = """
    SELECT a.attname, col_description(a.attrelid, a.attnum) AS comment
    FROM pg_attribute a
    WHERE a.attrelid = (%(schema)s || '.' || %(table)s)::regclass
      AND a.attnum > 0 AND NOT a.attisdropped
"""

_PK_SQL = """
    SELECT a.attname
    FROM pg_constraint con
    JOIN unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
    JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
    WHERE con.conrelid = (%(schema)s || '.' || %(table)s)::regclass AND con.contype = 'p'
    ORDER BY k.ord
"""

_FK_SQL = """
    SELECT
        af.attname AS local_column,
        cf.relname AS ref_table,
        ar.attname AS ref_column
    FROM pg_constraint con
    JOIN pg_class cf ON cf.oid = con.confrelid
    JOIN pg_attribute af ON af.attrelid = con.conrelid AND af.attnum = con.conkey[1]
    JOIN pg_attribute ar ON ar.attrelid = con.confrelid AND ar.attnum = con.confkey[1]
    WHERE con.conrelid = (%(schema)s || '.' || %(table)s)::regclass
      AND con.contype = 'f' AND array_length(con.conkey, 1) = 1
    ORDER BY af.attname
"""

_VIEW_COLUMNS_SQL = """
    SELECT table_name, column_name, data_type, is_nullable = 'YES' AS nullable, ordinal_position
    FROM information_schema.columns
    WHERE table_schema = %(schema)s AND table_name ~ '^v_'
    ORDER BY table_name, ordinal_position
"""

_GRANTED_VIEWS_SQL = """
    SELECT DISTINCT table_name
    FROM information_schema.role_table_grants
    WHERE grantee = %(role)s AND table_schema = %(schema)s AND privilege_type = 'SELECT'
"""


def introspect(conn: "psycopg.Connection", schema: str = "analytics", chatbot_role: str = "chatbot_ro") -> Snapshot:
    """Read the live catalog and return a Snapshot of exactly what
    `chatbot_role` can query. Raises SchemaDriftError on a view/grant
    mismatch rather than silently rendering a stale schema."""
    with conn.cursor() as cur:
        cur.execute(_GRANTED_VIEWS_SQL, {"role": chatbot_role, "schema": schema})
        granted = {row[0] for row in cur.fetchall()}

        cur.execute(_VIEW_COLUMNS_SQL, {"schema": schema})
        view_rows = cur.fetchall()

        cur.execute(_BASE_TABLE_SQL, {"schema": schema})
        base_tables = {
            row[0]: {"reltuples": row[1], "comment": row[2]} for row in cur.fetchall()
        }

        view_names = {row[0] for row in view_rows}
        if view_names != granted:
            missing_grant = view_names - granted
            extra_grant = granted - view_names
            raise SchemaDriftError(
                f"view set and chatbot_ro grants disagree — "
                f"views without a grant: {sorted(missing_grant) or 'none'}; "
                f"grants without a view: {sorted(extra_grant) or 'none'}"
            )

        columns_by_view: dict[str, list[tuple]] = {}
        for table_name, col_name, data_type, nullable, _ordinal in view_rows:
            columns_by_view.setdefault(table_name, []).append((col_name, data_type, nullable))

        relations: list[Relation] = []
        for view_name in sorted(columns_by_view):
            source_table = view_name.removeprefix("v_")
            base_meta = base_tables.get(source_table)

            col_comments: dict[str, str] = {}
            pk: tuple[str, ...] = ()
            fks: list[ForeignKey] = []
            source_note: str | None = None

            if base_meta is not None:
                cur.execute(_COLUMN_COMMENT_SQL, {"schema": schema, "table": source_table})
                col_comments = {name: comment for name, comment in cur.fetchall() if comment}

                cur.execute(_PK_SQL, {"schema": schema, "table": source_table})
                view_col_names = {c[0] for c in columns_by_view[view_name]}
                pk = tuple(r[0] for r in cur.fetchall() if r[0] in view_col_names)

                cur.execute(_FK_SQL, {"schema": schema, "table": source_table})
                for local_col, ref_table, ref_col in cur.fetchall():
                    if local_col in view_col_names:
                        fks.append(ForeignKey(local_col, ref_table, ref_col))

                source_note = f"columns/keys inferred from analytics.{source_table}"

            columns = tuple(
                Column(name, data_type, nullable, col_comments.get(name))
                for name, data_type, nullable in columns_by_view[view_name]
            )

            relations.append(
                Relation(
                    name=view_name,
                    comment=(base_meta or {}).get("comment"),
                    columns=columns,
                    primary_key=pk,
                    foreign_keys=tuple(fks),
                    row_magnitude=_row_magnitude((base_meta or {}).get("reltuples")),
                    source_note=source_note,
                )
            )

    from .render import render  # local import: render depends on these dataclasses

    captured_at = _now_iso()
    # Fingerprint over the rendered DDL, not the dataclasses — only
    # changes when what the model actually sees changes.
    provisional = Snapshot(schema=schema, relations=tuple(relations), captured_at=captured_at, fingerprint="")
    ddl = render(provisional)
    fingerprint = hashlib.sha256(ddl.encode("utf-8")).hexdigest()
    return Snapshot(schema=schema, relations=tuple(relations), captured_at=captured_at, fingerprint=fingerprint)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
