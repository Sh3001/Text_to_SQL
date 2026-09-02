"""Introspection against a live querywarden database. Skips (not fails)
when the database isn't reachable — see conftest.requires_db.
"""

from __future__ import annotations

from app.schema.introspect import Snapshot, introspect
from app.schema.render import render

from .conftest import requires_db

EXPECTED_VIEWS = {
    "v_campaign_events", "v_categories", "v_customers", "v_inventory",
    "v_marketing_campaigns", "v_order_items", "v_orders", "v_payments",
    "v_products", "v_refunds", "v_reviews", "v_shipments",
    "v_support_tickets", "v_warehouses",
}


@requires_db
def test_introspection_finds_exactly_the_granted_views(pg_conn):
    snap = introspect(pg_conn)
    assert snap.relation_names == EXPECTED_VIEWS


@requires_db
def test_legacy_trap_table_is_not_in_the_snapshot(pg_conn):
    # legacy_orders_flat has no view and no grant (db/00_schema.sql,
    # db/02_roles.sql) — it must not appear here regardless of what the
    # introspection query touches, or the model could learn about it from
    # the schema block even though it can never execute against it.
    snap = introspect(pg_conn)
    rendered = render(snap)
    assert "legacy_orders_flat" not in snap.relation_names
    assert "legacy_orders_flat" not in rendered


@requires_db
def test_orders_view_has_inferred_pk_and_fk(pg_conn):
    snap = introspect(pg_conn)
    orders = next(r for r in snap.relations if r.name == "v_orders")
    assert orders.primary_key == ("id",)
    fk_columns = {fk.column for fk in orders.foreign_keys}
    assert {"customer_id", "tenant_id"} <= fk_columns


@requires_db
def test_column_comments_survive_introspection(pg_conn):
    snap = introspect(pg_conn)
    orders = next(r for r in snap.relations if r.name == "v_orders")
    ordered_at = next(c for c in orders.columns if c.name == "ordered_at")
    assert ordered_at.comment is not None
    assert "business event" in ordered_at.comment.lower()


@requires_db
def test_row_magnitude_reflects_real_scale(pg_conn):
    snap = introspect(pg_conn)
    order_items = next(r for r in snap.relations if r.name == "v_order_items")
    tenants_backed = next(r for r in snap.relations if r.name == "v_warehouses")
    # order_items is seeded to ~500K rows, warehouses to 5 — must land in
    # different, correctly-ordered buckets.
    assert order_items.row_magnitude in {"~100K rows", "~1M rows"}
    assert tenants_backed.row_magnitude == "~100 rows"


@requires_db
def test_two_introspection_runs_render_byte_identical_ddl(pg_conn):
    # The actual end-to-end version of the render-determinism checkpoint:
    # two independent live introspection passes, not just two calls on the
    # same fixture object.
    snap_a = introspect(pg_conn)
    snap_b = introspect(pg_conn)
    assert render(snap_a) == render(snap_b)
    assert snap_a.fingerprint == snap_b.fingerprint


@requires_db
def test_no_foreign_key_references_an_unexposed_view(pg_conn):
    # Regression test for the tenants/v_tenants bug: every REFERENCES in
    # the rendered DDL must point at a relation that's actually in the
    # snapshot (and therefore actually queryable), never at a base table
    # that was never given a view.
    snap = introspect(pg_conn)
    ddl = render(snap)
    for line in ddl.splitlines():
        if "REFERENCES" in line:
            target = line.split("REFERENCES", 1)[1].strip().split("(")[0].strip()
            assert target.split(".")[-1] in snap.relation_names, f"dangling reference: {line!r}"


@requires_db
def test_fingerprint_is_a_function_of_rendered_ddl(pg_conn):
    snap = introspect(pg_conn)
    import hashlib

    assert snap.fingerprint == hashlib.sha256(render(snap).encode("utf-8")).hexdigest()
