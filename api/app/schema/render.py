"""Renders a Snapshot into the compact DDL text that goes in the model's
system prompt — behind the prompt-cache breakpoint (see the project plan's
cache layout). Determinism is the entire point of this module: the same
Snapshot must render to the same bytes every time, or every cache read
misses and the bill triples silently. That's why every loop below iterates
a tuple that was sorted at introspection time rather than a dict/set, and
why row counts are pre-bucketed (see introspect._row_magnitude) instead of
rendered as raw, drifting reltuples.
"""

from __future__ import annotations

from .introspect import Snapshot


def render(snapshot: Snapshot) -> str:
    lines: list[str] = [
        f"-- schema: {snapshot.schema} ({len(snapshot.relations)} tables, chatbot-queryable views only)",
        "",
    ]

    # Sorted here, defensively, rather than trusting the caller to have
    # sorted at introspection time — determinism is this module's one job,
    # so it shouldn't depend on an invariant it can't itself verify.
    for rel in sorted(snapshot.relations, key=lambda r: r.name):
        if rel.comment:
            lines.append(f"-- {rel.comment}")
        if rel.source_note:
            lines.append(f"-- {rel.source_note}")
        lines.append(f"TABLE {snapshot.schema}.{rel.name} (")

        col_lines = []
        for col in rel.columns:
            null_sql = "" if col.nullable else " NOT NULL"
            piece = f"    {col.name} {col.data_type}{null_sql}"
            col_lines.append(piece)
        for i, col in enumerate(rel.columns):
            suffix = "," if i < len(rel.columns) - 1 else ""
            line = col_lines[i] + suffix
            if col.comment:
                line += f"  -- {col.comment}"
            lines.append(line)

        lines.append(")")

        if rel.primary_key:
            lines.append(f"  PRIMARY KEY ({', '.join(rel.primary_key)})")
        for fk in rel.foreign_keys:
            ref_view = f"v_{fk.ref_relation}"
            if ref_view in snapshot.relation_names:
                lines.append(
                    f"  FOREIGN KEY ({fk.column}) REFERENCES {snapshot.schema}.{ref_view}({fk.ref_column})"
                )
            else:
                # The base table this FK points to (e.g. tenants) was never
                # given a chatbot-facing view — rendering the FK anyway
                # would point the model at a join target it can never
                # reach, which is worse than not mentioning the
                # relationship at all. See render.py's test for the bug
                # this guards: v_orders.tenant_id -> tenants(id) rendered
                # as "REFERENCES analytics.v_tenants(id)" before this
                # check existed, and no such view exists.
                lines.append(
                    f"  -- {fk.column} references analytics.{fk.ref_relation}, an internal table not exposed to this pipeline"
                )
        lines.append(f"  -- approx. row count: {rel.row_magnitude}")
        lines.append("")

    return "\n".join(lines)
