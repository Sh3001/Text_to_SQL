"""render() determinism — the Phase 02 checkpoint from the project plan:
"Rendered DDL is byte-identical across runs — the precondition for
caching to work at all." Runs on a hand-built fixture, no database, so
this property is checked on every CI run regardless of DB availability.
"""

from __future__ import annotations

from app.schema.introspect import Column, ForeignKey, Relation, Snapshot
from app.schema.render import render


def _fixture_snapshot() -> Snapshot:
    orders = Relation(
        name="v_orders",
        comment=None,
        columns=(
            Column("id", "bigint", False, None),
            Column("customer_id", "bigint", False, None),
            Column("status", "text", False, None),
            Column(
                "ordered_at", "timestamp with time zone", False,
                "Canonical business event time for an order. Prefer this over created_at.",
            ),
        ),
        primary_key=("id",),
        foreign_keys=(ForeignKey("customer_id", "customers", "id"),),
        row_magnitude="~100K rows",
        source_note="columns/keys inferred from analytics.orders",
    )
    customers = Relation(
        name="v_customers",
        comment=None,
        columns=(
            Column("id", "bigint", False, None),
            Column("name", "text", False, None),
        ),
        primary_key=("id",),
        foreign_keys=(),
        row_magnitude="~10K rows",
        source_note="columns/keys inferred from analytics.customers",
    )
    return Snapshot(
        schema="analytics",
        relations=(orders, customers),  # deliberately NOT alphabetical input order
        captured_at="2026-01-01T00:00:00+00:00",
        fingerprint="unused-in-this-test",
    )


def test_render_is_byte_identical_across_calls():
    snap = _fixture_snapshot()
    first = render(snap)
    second = render(snap)
    assert first == second


def test_render_is_byte_identical_across_independently_built_snapshots():
    # Two Snapshot objects built from scratch with identical data (not the
    # same Python object) must still render identically — this is the
    # actual property caching depends on, since every introspection run
    # builds a fresh object.
    assert render(_fixture_snapshot()) == render(_fixture_snapshot())


def test_render_does_not_embed_captured_at():
    # captured_at is metadata about the snapshot, not the schema — if it
    # leaked into the rendered text, two introspection runs one second
    # apart would never share a cache hit even with an unchanged schema.
    snap = _fixture_snapshot()
    assert snap.captured_at not in render(snap)


def test_row_magnitude_is_bucketed_not_raw():
    # A raw reltuples number would drift with every autovacuum/ANALYZE and
    # invalidate the cache on a schema that hasn't actually changed —
    # see introspect._row_magnitude. The rendered text should carry a
    # bucket label, never a bare large integer.
    ddl = render(_fixture_snapshot())
    assert "~100K rows" in ddl
    assert "~10K rows" in ddl


def test_relations_render_sorted_regardless_of_input_order():
    snap = _fixture_snapshot()  # customers, orders as given
    ddl = render(snap)
    assert ddl.index("v_customers") < ddl.index("v_orders")


def test_foreign_key_and_primary_key_lines_present():
    ddl = render(_fixture_snapshot())
    assert "PRIMARY KEY (id)" in ddl
    assert "FOREIGN KEY (customer_id) REFERENCES analytics.v_customers(id)" in ddl


def test_column_comment_is_rendered():
    ddl = render(_fixture_snapshot())
    assert "Prefer this over created_at" in ddl


def test_fk_to_an_unexposed_table_is_not_rendered_as_a_dangling_reference():
    # Regression test: v_orders.tenant_id really does have an FK to the
    # `tenants` base table, but there is no v_tenants view (db/00_schema.sql
    # never created one, and it's not granted to chatbot_ro). The renderer
    # used to emit "FOREIGN KEY (tenant_id) REFERENCES analytics.v_tenants
    # (id)" regardless — a join target the model can never actually reach.
    # It must fall back to a plain comment instead.
    orders = Relation(
        name="v_orders",
        comment=None,
        columns=(Column("id", "bigint", False, None), Column("tenant_id", "bigint", False, None)),
        primary_key=("id",),
        foreign_keys=(ForeignKey("tenant_id", "tenants", "id"),),
        row_magnitude="~100K rows",
        source_note=None,
    )
    snap = Snapshot(
        schema="analytics",
        relations=(orders,),  # no v_tenants relation in this snapshot at all
        captured_at="2026-01-01T00:00:00+00:00",
        fingerprint="unused",
    )
    ddl = render(snap)
    assert "REFERENCES analytics.v_tenants" not in ddl
    assert "tenant_id references analytics.tenants, an internal table not exposed" in ddl
