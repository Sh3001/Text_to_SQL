"""Wires a real, live-introspected Catalog into the Phase 01 AST guard —
the integration point promised in guards/ast_guard.py's Catalog protocol
and left unexercised until now (Phase 01's tests all passed catalog=None).
"""

from __future__ import annotations

from app.guards.ast_guard import check
from app.guards.errors import RejectReason
from app.schema.catalog import SnapshotCatalog
from app.schema.introspect import introspect

from .conftest import requires_db


@requires_db
def test_known_view_is_approved_with_catalog(pg_conn):
    catalog = SnapshotCatalog(introspect(pg_conn))
    result = check("SELECT id FROM analytics.v_orders", catalog=catalog)
    assert result.ok is True


@requires_db
def test_known_view_unqualified_is_approved_with_catalog(pg_conn):
    # chatbot_ro's search_path is pinned to analytics (db/02_roles.sql), so
    # an unqualified relation name is just as legitimate as a qualified one.
    catalog = SnapshotCatalog(introspect(pg_conn))
    result = check("SELECT id FROM v_orders", catalog=catalog)
    assert result.ok is True


@requires_db
def test_hallucinated_table_is_rejected_with_a_suggestion(pg_conn):
    catalog = SnapshotCatalog(introspect(pg_conn))
    # A plausible model mistake: guessing the table is named v_order
    # (singular) rather than v_orders.
    result = check("SELECT id FROM analytics.v_order", catalog=catalog)
    assert result.ok is False
    assert result.reason == RejectReason.UNKNOWN_TABLE
    assert "v_orders" in result.detail


@requires_db
def test_legacy_table_is_rejected_as_unknown_not_merely_ungranted(pg_conn):
    # The catalog check fires on parse/plan, before the query would ever
    # reach the database to hit the real permission error — a faster,
    # more specific rejection than waiting for chatbot_ro's grant to fail.
    catalog = SnapshotCatalog(introspect(pg_conn))
    result = check("SELECT * FROM analytics.legacy_orders_flat", catalog=catalog)
    assert result.ok is False
    assert result.reason == RejectReason.UNKNOWN_TABLE


@requires_db
def test_base_table_is_rejected_as_unknown_only_views_are_in_catalog(pg_conn):
    # analytics.orders (the base table) is a real relation in the database
    # but chatbot_ro can only ever reach the view — the catalog reflects
    # that, so a model that hallucinates the base-table name (very
    # plausible: it's the more "natural" name) gets caught here too.
    catalog = SnapshotCatalog(introspect(pg_conn))
    result = check("SELECT * FROM analytics.orders", catalog=catalog)
    assert result.ok is False
    assert result.reason == RejectReason.UNKNOWN_TABLE


@requires_db
def test_adversarial_suite_still_blocked_with_a_real_catalog_attached(pg_conn):
    # The catalog is additive, not a replacement — every Phase 01 safety
    # violation must still be caught even once a real Catalog is wired in.
    from .test_ast_guard import ADVERSARIAL_SUITE

    catalog = SnapshotCatalog(introspect(pg_conn))
    for sql in ADVERSARIAL_SUITE:
        result = check(sql, catalog=catalog)
        assert result.ok is False, f"catalog-aware guard let through: {sql!r}"
