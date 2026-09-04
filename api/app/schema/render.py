"""Renders a Snapshot into the compact DDL text for the model's system
prompt. Determinism is the whole point: the same Snapshot must render to
the same bytes every time, or the prompt cache misses silently."""

from __future__ import annotations

from .introspect import Snapshot


def render(snapshot: Snapshot) -> str:
    lines: list[str] = [
        f"-- schema: {snapshot.schema} ({len(snapshot.relations)} tables, chatbot-queryable views only)",
        "",
    ]

    # Sorted defensively rather than trusting the caller.
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
                # The base table this FK points to has no chatbot-facing
                # view — pointing the model at an unreachable join target
                # is worse than not mentioning the relationship at all.
                lines.append(
                    f"  -- {fk.column} references analytics.{fk.ref_relation}, an internal table not exposed to this pipeline"
                )
        lines.append(f"  -- approx. row count: {rel.row_magnitude}")
        lines.append("")

    return "\n".join(lines)
