-- Query Warden — the floor. Even if every other defense layer has a bug,
-- a compromised query still cannot write, because chatbot_ro was never
-- granted the privilege. See the checkpoint at the bottom.

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

-- SELECT on the view set only — never the base tables.
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

-- analytics.legacy_orders_flat has no grant at all — chatbot_ro can't see
-- it regardless of what schema retrieval surfaces.
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics REVOKE ALL ON TABLES FROM chatbot_ro;

-- Session-level guardrails, applied every time this role connects.

ALTER ROLE chatbot_ro SET statement_timeout                   = '15s';
ALTER ROLE chatbot_ro SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE chatbot_ro SET lock_timeout                        = '3s';
ALTER ROLE chatbot_ro SET default_transaction_read_only       = on;
ALTER ROLE chatbot_ro SET work_mem                             = '32MB';
ALTER ROLE chatbot_ro SET search_path                          = analytics;

-- RLS on the base tables — a backstop, not the primary mechanism (that's
-- the view's own WHERE clause; see db/00_schema.sql for why RLS alone
-- would silently do nothing here). Exists for a future role/migration
-- with a direct GRANT on the base table. Inert today (single tenant),
-- shipped now because retrofitting later is riskier.

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

-- The API sets app.tenant_id via set_config() at the top of every request
-- (api/app/pipeline/execute.py) — the same GUC the views read.

-- ---------------------------------------------------------------------------
-- Checkpoint — verified against a live database:
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
