-- Query Warden — the floor.
--
-- Everything upstream of this file (the AST guard, the plan-cost budget,
-- the execution envelope) is defense in depth. This file is the layer that
-- has to be right on its own: even if every other layer has a bug, a
-- compromised query still cannot write, because the role it runs as has
-- never been granted the privilege. Prove this file with psql before
-- writing a line of model code — see the checkpoint at the bottom.

-- ---------------------------------------------------------------------------
-- The role the entire generated-SQL pipeline connects as. Nothing else in
-- this system ever authenticates as anything more privileged.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'chatbot_ro') THEN
        CREATE ROLE chatbot_ro LOGIN PASSWORD 'chatbot_ro'
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION;
    END IF;
END
$$;

-- Deny by default, then grant exactly what's needed. REVOKE on a schema
-- with no prior GRANT is a no-op, not an error — safe to run idempotently.
REVOKE ALL ON SCHEMA public FROM chatbot_ro;
REVOKE ALL ON SCHEMA analytics FROM chatbot_ro;
REVOKE ALL ON SCHEMA audit FROM chatbot_ro;

GRANT USAGE ON SCHEMA analytics TO chatbot_ro;

-- SELECT on the view set only — never the base tables. This is what makes
-- the semantic layer (soft-delete filtering, column naming) a guarantee
-- instead of a prompt suggestion: the model has no path to a column or row
-- that isn't already filtered the way the business means it.
GRANT SELECT ON
    analytics.v_customers,
    analytics.v_products,
    analytics.v_orders,
    analytics.v_order_items,
    analytics.v_payments,
    analytics.v_refunds,
    analytics.v_shipments,
    analytics.v_reviews,
    analytics.v_inventory,
    analytics.v_support_tickets,
    analytics.v_categories,
    analytics.v_warehouses,
    analytics.v_marketing_campaigns,
    analytics.v_campaign_events
TO chatbot_ro;

-- Note what is absent: analytics.legacy_orders_flat has no grant at all.
-- No view, no SELECT, nothing — it does not exist as far as chatbot_ro
-- is concerned, regardless of what the model's schema retrieval surfaces.

-- Future writes to this schema don't accidentally become visible either.
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics REVOKE ALL ON TABLES FROM chatbot_ro;

-- audit.* is where rejected/blocked statements get logged (Phase 04+).
-- chatbot_ro must never be able to read or tamper with its own audit trail.
-- (No GRANT statement here at all — the REVOKE ALL above already covers it;
-- this comment exists so a future migration doesn't "helpfully" add one.)

-- ---------------------------------------------------------------------------
-- Session-level guardrails, applied every time this role connects —
-- independent of anything the application code remembers to set.
-- ---------------------------------------------------------------------------

ALTER ROLE chatbot_ro SET statement_timeout                   = '15s';
ALTER ROLE chatbot_ro SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE chatbot_ro SET lock_timeout                        = '3s';
ALTER ROLE chatbot_ro SET default_transaction_read_only       = on;
ALTER ROLE chatbot_ro SET work_mem                             = '32MB';
ALTER ROLE chatbot_ro SET search_path                          = analytics;

-- ---------------------------------------------------------------------------
-- Row-level security on the base tables — a backstop, not the primary
-- mechanism. The primary tenant filter lives in the view definitions
-- themselves (db/00_schema.sql), because chatbot_ro never touches these
-- base tables directly (see the checkpoint below) and because RLS alone
-- would silently do nothing here: views execute with the OWNER's
-- privileges, the owner in this setup is a superuser, and superusers
-- bypass row security unconditionally — FORCE ROW LEVEL SECURITY included.
-- These policies exist for the scenario the view can't cover: some future
-- role or migration that ends up with a direct GRANT on the base table.
--
-- The seed data ships a single tenant (id = 1), so none of this is
-- exercised today — it's here because retrofitting tenant isolation after
-- real tenants exist is a far riskier migration than shipping it from day
-- one, even in its currently-inert form.
-- ---------------------------------------------------------------------------

ALTER TABLE analytics.customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE analytics.orders    ENABLE ROW LEVEL SECURITY;
ALTER TABLE analytics.products  ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON analytics.customers;
CREATE POLICY tenant_isolation ON analytics.customers FOR SELECT TO chatbot_ro
    USING (tenant_id = current_setting('app.tenant_id', true)::bigint);

DROP POLICY IF EXISTS tenant_isolation ON analytics.orders;
CREATE POLICY tenant_isolation ON analytics.orders FOR SELECT TO chatbot_ro
    USING (tenant_id = current_setting('app.tenant_id', true)::bigint);

DROP POLICY IF EXISTS tenant_isolation ON analytics.products;
CREATE POLICY tenant_isolation ON analytics.products FOR SELECT TO chatbot_ro
    USING (tenant_id = current_setting('app.tenant_id', true)::bigint);

-- The API sets `SET LOCAL app.tenant_id = '<id>'` at the top of every
-- request transaction (see api/app/pipeline/execute.py, Phase 04) — the
-- same GUC the views themselves read. If it's never set, current_setting
-- (..., true) returns NULL and the view's own filter fails closed: an
-- unset tenant sees zero rows through v_orders/v_customers/v_products/
-- v_order_items, never every tenant's.

-- ---------------------------------------------------------------------------
-- Checkpoint (Phase 00) — verified against a live database:
--
--   psql "$CHATBOT_DATABASE_URL" -c "DELETE FROM analytics.orders"
--       -> ERROR: cannot execute DELETE in a read-only transaction
--   psql "$CHATBOT_DATABASE_URL" -c "CREATE TABLE x (id int)"
--       -> ERROR: cannot execute CREATE TABLE in a read-only transaction
--   psql "$CHATBOT_DATABASE_URL" -c "SELECT * FROM analytics.legacy_orders_flat"
--       -> ERROR: permission denied for table legacy_orders_flat
--   psql "$CHATBOT_DATABASE_URL" -c "SELECT * FROM analytics.orders"
--       -> ERROR: permission denied for table orders   (views only, never base tables)
--   psql "$CHATBOT_DATABASE_URL" -c "SELECT * FROM pg_shadow"
--       -> ERROR: permission denied for view pg_shadow
--   psql "$CHATBOT_DATABASE_URL" -c "SELECT count(*) FROM analytics.v_customers"
--       -> 0   (no app.tenant_id set — fails closed, not open)
--   psql "$CHATBOT_DATABASE_URL" \
--       -c "SET app.tenant_id = '1'" -c "SELECT count(*) FROM analytics.v_orders"
--       -> a real, tenant-1-scoped number
-- ---------------------------------------------------------------------------
