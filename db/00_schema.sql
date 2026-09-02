-- Query Warden — analytics schema
--
-- A marketplace warehouse, seeded deterministically (see 01_seed.sql).
-- Deliberately includes the traps a text-to-SQL system has to survive:
--   * two plausible "date" columns on orders (created_at vs. ordered_at)
--   * a soft-delete flag every naive COUNT(*) gets wrong
--   * a multi-currency amount column with no implicit conversion
--   * a legacy denormalized table that still has rows and must never be joined
--
-- chatbot_ro (db/02_roles.sql) is granted SELECT on the views in this file,
-- never on the base tables — the semantic layer is enforced by the grant,
-- not merely suggested by a prompt.

CREATE SCHEMA IF NOT EXISTS analytics;
CREATE SCHEMA IF NOT EXISTS audit;   -- append-only guard/query log; chatbot_ro has no grants here at all

SET search_path = analytics, public;

-- Postgres ships pg_trgm for the fuzzy value index (Phase 04).
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- Reference data
-- ---------------------------------------------------------------------------

CREATE TABLE analytics.tenants (
    id          bigint PRIMARY KEY,
    name        text NOT NULL
);
COMMENT ON TABLE analytics.tenants IS 'One row per customer org. RLS on orders/customers keys off tenant_id.';

CREATE TABLE analytics.categories (
    id          bigserial PRIMARY KEY,
    name        text NOT NULL,
    parent_id   bigint REFERENCES analytics.categories(id)
);

CREATE TABLE analytics.warehouses (
    id          bigserial PRIMARY KEY,
    name        text NOT NULL,
    region      text NOT NULL
);

-- ---------------------------------------------------------------------------
-- Core entities
-- ---------------------------------------------------------------------------

CREATE TABLE analytics.customers (
    id          bigserial PRIMARY KEY,
    tenant_id   bigint NOT NULL REFERENCES analytics.tenants(id),
    name        text NOT NULL,
    email       text NOT NULL,
    region      text NOT NULL,
    state       text,               -- e.g. 'CA' — trap for "California" in the value index
    country     text NOT NULL DEFAULT 'US',
    signup_at   timestamptz NOT NULL,
    is_deleted  boolean NOT NULL DEFAULT false
);
COMMENT ON COLUMN analytics.customers.is_deleted IS 'Soft-delete flag. A bare COUNT(*) over this table double-counts churned accounts.';

CREATE TABLE analytics.products (
    id              bigserial PRIMARY KEY,
    tenant_id       bigint NOT NULL REFERENCES analytics.tenants(id),
    category_id     bigint NOT NULL REFERENCES analytics.categories(id),
    sku             text NOT NULL,
    name            text NOT NULL,
    unit_price_usd  numeric(12,2) NOT NULL,
    currency        text NOT NULL DEFAULT 'USD',   -- listing currency; see orders.currency for the trap
    is_deleted      boolean NOT NULL DEFAULT false
);

CREATE TABLE analytics.orders (
    id          bigserial PRIMARY KEY,
    tenant_id   bigint NOT NULL REFERENCES analytics.tenants(id),
    customer_id bigint NOT NULL REFERENCES analytics.customers(id),
    status      text NOT NULL,   -- 'pending' | 'paid' | 'shipped' | 'delivered' | 'cancelled' | 'refunded'
    currency    text NOT NULL DEFAULT 'USD',
    created_at  timestamptz NOT NULL,   -- row insert time — NOT the business date
    ordered_at  timestamptz NOT NULL,   -- when the customer actually placed the order
    is_deleted  boolean NOT NULL DEFAULT false
);
COMMENT ON COLUMN analytics.orders.created_at IS 'Row insert timestamp. Use ordered_at for revenue-by-date questions, not this.';
COMMENT ON COLUMN analytics.orders.ordered_at IS 'Canonical business event time for an order. Prefer this over created_at.';
COMMENT ON COLUMN analytics.orders.currency IS 'Order-level currency. Amounts in order_items/payments/refunds are in THIS currency, not USD — no implicit conversion exists.';

CREATE TABLE analytics.order_items (
    id              bigserial PRIMARY KEY,
    order_id        bigint NOT NULL REFERENCES analytics.orders(id),
    product_id      bigint NOT NULL REFERENCES analytics.products(id),
    quantity        integer NOT NULL,
    unit_price      numeric(12,2) NOT NULL,   -- in orders.currency for this order_id
    discount_pct    numeric(5,2) NOT NULL DEFAULT 0
);

CREATE TABLE analytics.payments (
    id          bigserial PRIMARY KEY,
    order_id    bigint NOT NULL REFERENCES analytics.orders(id),
    amount      numeric(12,2) NOT NULL,   -- in orders.currency
    currency    text NOT NULL,
    method      text NOT NULL,            -- 'card' | 'paypal' | 'bank_transfer'
    status      text NOT NULL,            -- 'succeeded' | 'failed' | 'pending'
    paid_at     timestamptz
);

CREATE TABLE analytics.refunds (
    id          bigserial PRIMARY KEY,
    order_id    bigint NOT NULL REFERENCES analytics.orders(id),
    amount      numeric(12,2) NOT NULL,
    currency    text NOT NULL,
    reason      text NOT NULL,
    refunded_at timestamptz NOT NULL
);
COMMENT ON TABLE analytics.refunds IS 'Revenue questions must subtract refunds — orders.status = ''refunded'' alone undercounts partial refunds.';

CREATE TABLE analytics.shipments (
    id              bigserial PRIMARY KEY,
    order_id        bigint NOT NULL REFERENCES analytics.orders(id),
    carrier         text NOT NULL,
    status          text NOT NULL,   -- 'label_created' | 'in_transit' | 'delivered' | 'returned'
    shipped_at      timestamptz,
    delivered_at    timestamptz
);

CREATE TABLE analytics.reviews (
    id          bigserial PRIMARY KEY,
    product_id  bigint NOT NULL REFERENCES analytics.products(id),
    customer_id bigint NOT NULL REFERENCES analytics.customers(id),
    rating      smallint NOT NULL CHECK (rating BETWEEN 1 AND 5),
    body        text,
    created_at  timestamptz NOT NULL
);

CREATE TABLE analytics.inventory (
    warehouse_id    bigint NOT NULL REFERENCES analytics.warehouses(id),
    product_id      bigint NOT NULL REFERENCES analytics.products(id),
    qty_on_hand     integer NOT NULL,
    updated_at      timestamptz NOT NULL,
    PRIMARY KEY (warehouse_id, product_id)
);

CREATE TABLE analytics.support_tickets (
    id          bigserial PRIMARY KEY,
    customer_id bigint NOT NULL REFERENCES analytics.customers(id),
    order_id    bigint REFERENCES analytics.orders(id),
    status      text NOT NULL,   -- 'open' | 'pending' | 'closed'
    opened_at   timestamptz NOT NULL,
    closed_at   timestamptz
);

CREATE TABLE analytics.marketing_campaigns (
    id          bigserial PRIMARY KEY,
    name        text NOT NULL,
    channel     text NOT NULL,   -- 'email' | 'search' | 'social' | 'affiliate'
    starts_at   timestamptz NOT NULL,
    ends_at     timestamptz NOT NULL,
    budget_usd  numeric(12,2) NOT NULL
);

CREATE TABLE analytics.campaign_events (
    id              bigserial PRIMARY KEY,
    campaign_id     bigint NOT NULL REFERENCES analytics.marketing_campaigns(id),
    customer_id     bigint NOT NULL REFERENCES analytics.customers(id),
    event_type      text NOT NULL,   -- 'impression' | 'click' | 'conversion'
    occurred_at     timestamptz NOT NULL
);

-- ---------------------------------------------------------------------------
-- The trap: a legacy denormalized table that still has rows.
-- Never granted to chatbot_ro, never listed in semantic/catalog.yml's
-- allowed join paths — it exists so the eval harness can catch a model that
-- discovers it via schema retrieval and joins on it anyway.
-- ---------------------------------------------------------------------------

CREATE TABLE analytics.legacy_orders_flat (
    order_id        bigint PRIMARY KEY,
    customer_name   text,
    customer_email  text,
    total_amount    numeric(12,2),
    order_date      date,
    status_code     integer   -- undocumented: 1=pending 2=paid 3=shipped 4=cancelled — no lookup table exists
);
COMMENT ON TABLE analytics.legacy_orders_flat IS 'DEPRECATED — pre-2019 denormalized export, superseded by orders/order_items. Not maintained. Do not query.';

-- ---------------------------------------------------------------------------
-- Indexes (also what a real EXPLAIN plan will lean on for the cost guard)
-- ---------------------------------------------------------------------------

CREATE INDEX idx_customers_tenant       ON analytics.customers(tenant_id);
CREATE INDEX idx_customers_state_trgm   ON analytics.customers USING gin (state gin_trgm_ops);
CREATE INDEX idx_products_tenant        ON analytics.products(tenant_id);
CREATE INDEX idx_products_category      ON analytics.products(category_id);
CREATE INDEX idx_orders_tenant          ON analytics.orders(tenant_id);
CREATE INDEX idx_orders_customer        ON analytics.orders(customer_id);
CREATE INDEX idx_orders_ordered_at      ON analytics.orders(ordered_at);
CREATE INDEX idx_orders_created_at      ON analytics.orders(created_at);
CREATE INDEX idx_orders_status_trgm     ON analytics.orders USING gin (status gin_trgm_ops);
CREATE INDEX idx_order_items_order      ON analytics.order_items(order_id);
CREATE INDEX idx_order_items_product    ON analytics.order_items(product_id);
CREATE INDEX idx_payments_order         ON analytics.payments(order_id);
CREATE INDEX idx_refunds_order          ON analytics.refunds(order_id);
CREATE INDEX idx_shipments_order        ON analytics.shipments(order_id);
CREATE INDEX idx_reviews_product        ON analytics.reviews(product_id);
CREATE INDEX idx_support_customer       ON analytics.support_tickets(customer_id);
CREATE INDEX idx_campaign_events_camp   ON analytics.campaign_events(campaign_id);
CREATE INDEX idx_campaign_events_cust   ON analytics.campaign_events(customer_id);

-- ---------------------------------------------------------------------------
-- Views — this is the surface chatbot_ro is actually granted.
-- Each pre-filters soft-deleted rows and exposes only the columns the
-- semantic layer sanctions, so the model cannot see what it isn't supposed
-- to use even if it hallucinates a request for it.
-- ---------------------------------------------------------------------------

-- Tenant scoping lives in the view's own WHERE clause, not in RLS on the
-- base table. Views execute with the OWNER's privileges by default, and
-- the schema owner here is a superuser (or at minimum the table owner) —
-- both bypass row-level security unconditionally, FORCE ROW LEVEL SECURITY
-- included. RLS on the base tables (below) is still enabled as a backstop
-- for any future role that gets direct table access, but it is not what
-- protects chatbot_ro, which only ever goes through these views. The
-- filter below is: current_setting(..., true) returns NULL when
-- app.tenant_id was never set, so `tenant_id = NULL` is NULL (not true)
-- for every row — an unset tenant sees zero rows, not every tenant's.

CREATE VIEW analytics.v_customers AS
    SELECT id, tenant_id, name, email, region, state, country, signup_at
    FROM analytics.customers
    WHERE is_deleted = false
      AND tenant_id = current_setting('app.tenant_id', true)::bigint;

CREATE VIEW analytics.v_products AS
    SELECT id, tenant_id, category_id, sku, name, unit_price_usd, currency
    FROM analytics.products
    WHERE is_deleted = false
      AND tenant_id = current_setting('app.tenant_id', true)::bigint;

CREATE VIEW analytics.v_orders AS
    SELECT id, tenant_id, customer_id, status, currency, created_at, ordered_at
    FROM analytics.orders
    WHERE is_deleted = false
      AND tenant_id = current_setting('app.tenant_id', true)::bigint;

CREATE VIEW analytics.v_order_items AS
    SELECT oi.id, oi.order_id, oi.product_id, oi.quantity, oi.unit_price, oi.discount_pct
    FROM analytics.order_items oi
    JOIN analytics.orders o ON o.id = oi.order_id
    WHERE o.is_deleted = false
      AND o.tenant_id = current_setting('app.tenant_id', true)::bigint;

CREATE VIEW analytics.v_payments        AS SELECT * FROM analytics.payments;
CREATE VIEW analytics.v_refunds         AS SELECT * FROM analytics.refunds;
CREATE VIEW analytics.v_shipments       AS SELECT * FROM analytics.shipments;
CREATE VIEW analytics.v_reviews         AS SELECT * FROM analytics.reviews;
CREATE VIEW analytics.v_inventory       AS SELECT * FROM analytics.inventory;
CREATE VIEW analytics.v_support_tickets AS SELECT * FROM analytics.support_tickets;
CREATE VIEW analytics.v_categories      AS SELECT * FROM analytics.categories;
CREATE VIEW analytics.v_warehouses      AS SELECT * FROM analytics.warehouses;
CREATE VIEW analytics.v_marketing_campaigns AS SELECT * FROM analytics.marketing_campaigns;
CREATE VIEW analytics.v_campaign_events     AS SELECT * FROM analytics.campaign_events;

-- Note: legacy_orders_flat has no view and no grant. It is reachable only by
-- someone with base-table access — which chatbot_ro will never have.
