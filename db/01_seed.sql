-- Query Warden — deterministic seed (generate_series + setseed(0.42)).
--
-- Gotcha: Postgres can evaluate a volatile expression (random(), now())
-- ONCE and reuse it across every row, even inside a LATERAL or WHERE, if
-- that expression has no correlated column reference. Every multi-column
-- CTE below exists to force per-row evaluation by computing randomness
-- directly in a CTE's own top-level SELECT list off a real row source.

SET search_path = analytics, public;
SELECT setseed(0.42);

-- Scale knobs -----------------------------------------------------------
\set n_customers 20000
\set n_products 2000
\set n_orders 200000
\set n_campaigns 40
\set n_campaign_events 50000

-- Reference data

INSERT INTO analytics.tenants (id, name) VALUES (1, 'Acme Marketplace');

INSERT INTO analytics.warehouses (name, region)
VALUES ('US-East DC1', 'us-east'), ('US-West DC2', 'us-west'),
       ('EU-Central DC3', 'eu-central'), ('EU-West DC4', 'eu-west'),
       ('APAC DC5', 'apac');

-- Two-level category tree
INSERT INTO analytics.categories (name, parent_id)
SELECT c, NULL FROM unnest(ARRAY['Electronics','Home & Garden','Apparel','Sporting Goods','Books & Media','Toys & Games']) c;

INSERT INTO analytics.categories (name, parent_id)
SELECT sub.name, p.id
FROM (VALUES
    ('Laptops','Electronics'),('Headphones','Electronics'),('Cameras','Electronics'),
    ('Furniture','Home & Garden'),('Kitchen','Home & Garden'),('Outdoor','Home & Garden'),
    ('Mens','Apparel'),('Womens','Apparel'),('Kids','Apparel'),
    ('Fitness','Sporting Goods'),('Cycling','Sporting Goods'),
    ('Fiction','Books & Media'),('Non-Fiction','Books & Media'),
    ('Board Games','Toys & Games'),('Action Figures','Toys & Games')
) AS sub(name, parent_name)
JOIN analytics.categories p ON p.name = sub.parent_name AND p.parent_id IS NULL;

-- Customers — region/state/country deliberately noisy for the value index.

INSERT INTO analytics.customers (tenant_id, name, email, region, state, country, signup_at, is_deleted)
SELECT
    1,
    'Customer ' || i,
    'customer' || i || '@example.com',
    (ARRAY['north-america','europe','apac','latam'])[1 + floor(random()*4)::int],
    CASE WHEN random() < 0.55
         THEN (ARRAY['CA','NY','TX','WA','MA','IL','FL','CO'])[1 + floor(random()*8)::int]
         ELSE NULL END,
    CASE WHEN random() < 0.7 THEN 'US'
         WHEN random() < 0.85 THEN 'CA'
         ELSE (ARRAY['GB','DE','FR','JP','AU'])[1 + floor(random()*5)::int] END,
    now() - (random() * 1460 || ' days')::interval,
    random() < 0.04   -- ~4% soft-deleted (churned/GDPR-deleted) accounts
FROM generate_series(1, :n_customers) i;

-- Products — category assigned via a numbered join, not
-- `ORDER BY random() LIMIT 1` in a scalar subquery (same gotcha as above).

WITH leaf_categories AS (
    SELECT id, row_number() OVER (ORDER BY id) AS rn
    FROM analytics.categories WHERE parent_id IS NOT NULL
), category_count AS (
    SELECT count(*) AS n FROM leaf_categories
), product_base AS (
    SELECT i, (1 + floor(random() * (SELECT n FROM category_count)))::int AS cat_rn
    FROM generate_series(1, :n_products) i
)
INSERT INTO analytics.products (tenant_id, category_id, sku, name, unit_price_usd, currency, is_deleted)
SELECT
    1,
    lc.id,
    'SKU-' || lpad(pb.i::text, 6, '0'),
    'Product ' || pb.i,
    round((5 + random() * 995)::numeric, 2),
    'USD',
    random() < 0.03
FROM product_base pb
JOIN leaf_categories lc ON lc.rn = pb.cat_rn;

-- Orders — the created_at / ordered_at gap is deliberate (up to 40h);
-- currency varies so amounts can't be summed across orders without a
-- per-row currency check.

WITH order_base AS (
    SELECT
        i,
        (1 + floor(random() * :n_customers))::bigint AS customer_id,
        (ARRAY['pending','paid','shipped','delivered','cancelled','refunded'])
            [1 + floor(random() * 6)::int] AS status,
        CASE WHEN random() < 0.75 THEN 'USD'
             WHEN random() < 0.9  THEN 'EUR'
             ELSE (ARRAY['GBP','CAD','JPY'])[1 + floor(random()*3)::int] END AS currency,
        now() - (random() * 730 || ' days')::interval AS ordered_at,
        random() < 0.015 AS is_deleted
    FROM generate_series(1, :n_orders) i
)
INSERT INTO analytics.orders (tenant_id, customer_id, status, currency, created_at, ordered_at, is_deleted)
SELECT
    1,
    customer_id,
    status,
    currency,
    ordered_at + (random() * 40 || ' hours')::interval AS created_at,   -- row lands after the business event
    ordered_at,
    is_deleted
FROM order_base;

-- Order items — 1 to 4 lines per order (~500k rows at default scale).

WITH item_counts AS (
    SELECT id AS order_id, (1 + floor(random() * 4))::int AS n_items
    FROM analytics.orders
)
INSERT INTO analytics.order_items (order_id, product_id, quantity, unit_price, discount_pct)
SELECT
    ic.order_id,
    (1 + floor(random() * :n_products))::bigint,
    (1 + floor(random() * 4))::int,
    round((5 + random() * 995)::numeric, 2),
    CASE WHEN random() < 0.2 THEN round((random()*25)::numeric, 2) ELSE 0 END
FROM item_counts ic
CROSS JOIN LATERAL generate_series(1, ic.n_items) AS line;

-- Payments — one attempt per order, occasionally a retry after a decline.

INSERT INTO analytics.payments (order_id, amount, currency, method, status, paid_at)
SELECT
    o.id,
    ot.total,
    o.currency,
    (ARRAY['card','paypal','bank_transfer'])[1 + floor(random()*3)::int],
    CASE WHEN o.status IN ('pending') AND random() < 0.4 THEN 'pending'
         WHEN random() < 0.03 THEN 'failed'
         ELSE 'succeeded' END,
    CASE WHEN o.status = 'pending' AND random() < 0.4 THEN NULL ELSE o.ordered_at + interval '5 minutes' END
FROM analytics.orders o
JOIN LATERAL (
    SELECT COALESCE(SUM(unit_price * quantity * (1 - discount_pct/100.0)), 0) AS total
    FROM analytics.order_items oi WHERE oi.order_id = o.id
) ot ON true;

-- Refunds — only on a slice of orders, partial amounts.

INSERT INTO analytics.refunds (order_id, amount, currency, reason, refunded_at)
SELECT
    o.id,
    round((p.amount * (0.2 + random()*0.8))::numeric, 2),   -- partial refund
    o.currency,
    (ARRAY['damaged','wrong_item','changed_mind','late_delivery','duplicate_charge'])[1 + floor(random()*5)::int],
    o.ordered_at + (random()*20 || ' days')::interval
FROM analytics.orders o
JOIN analytics.payments p ON p.order_id = o.id AND p.status = 'succeeded'
WHERE random() < 0.07;

-- Shipments

INSERT INTO analytics.shipments (order_id, carrier, status, shipped_at, delivered_at)
SELECT
    o.id,
    (ARRAY['ups','fedex','usps','dhl'])[1 + floor(random()*4)::int],
    CASE WHEN o.status = 'delivered' THEN 'delivered'
         WHEN o.status = 'shipped' THEN 'in_transit'
         WHEN o.status = 'cancelled' THEN 'returned'
         ELSE 'label_created' END,
    CASE WHEN o.status IN ('shipped','delivered') THEN o.ordered_at + interval '1 day' ELSE NULL END,
    CASE WHEN o.status = 'delivered' THEN o.ordered_at + (2 + random()*5 || ' days')::interval ELSE NULL END
FROM analytics.orders o
WHERE o.status IN ('paid','shipped','delivered','cancelled') AND random() < 0.98;

-- Reviews — a subset of delivered order_items get reviewed by the buyer.

INSERT INTO analytics.reviews (product_id, customer_id, rating, body, created_at)
SELECT DISTINCT ON (oi.product_id, o.customer_id)
    oi.product_id,
    o.customer_id,
    (1 + floor(random()*5))::smallint,
    (ARRAY['Great value.','Not what I expected.','Works as advertised.',
           'Fast shipping, good product.','Would not buy again.', NULL])[1 + floor(random()*6)::int],
    o.ordered_at + (5 + random()*30 || ' days')::interval
FROM analytics.order_items oi
JOIN analytics.orders o ON o.id = oi.order_id
WHERE o.status = 'delivered' AND random() < 0.4;

-- Inventory — every warehouse stocks every product at some level.

INSERT INTO analytics.inventory (warehouse_id, product_id, qty_on_hand, updated_at)
SELECT w.id, p.id, floor(random()*500)::int, now() - (random()*10 || ' days')::interval
FROM analytics.warehouses w
CROSS JOIN analytics.products p;

-- Support tickets — skewed toward cancelled/refunded orders.

INSERT INTO analytics.support_tickets (customer_id, order_id, status, opened_at, closed_at)
SELECT
    o.customer_id, o.id,
    (ARRAY['open','pending','closed'])[1 + floor(random()*3)::int],
    o.ordered_at + (1 + random()*10 || ' days')::interval,
    CASE WHEN random() < 0.75 THEN o.ordered_at + (2 + random()*15 || ' days')::interval ELSE NULL END
FROM analytics.orders o
WHERE (o.status IN ('cancelled','refunded') AND random() < 0.35)
   OR random() < 0.02;

-- Marketing campaigns + funnel events.

WITH campaign_base AS (
    SELECT i, now() - (random()*700 || ' days')::interval AS starts_at
    FROM generate_series(1, :n_campaigns) i
)
INSERT INTO analytics.marketing_campaigns (name, channel, starts_at, ends_at, budget_usd)
SELECT
    'Campaign ' || i,
    (ARRAY['email','search','social','affiliate'])[1 + floor(random()*4)::int],
    starts_at, starts_at + interval '30 days',
    round((1000 + random()*49000)::numeric, 2)
FROM campaign_base;

INSERT INTO analytics.campaign_events (campaign_id, customer_id, event_type, occurred_at)
SELECT
    (1 + floor(random()*:n_campaigns))::bigint,
    (1 + floor(random()*:n_customers))::bigint,
    (ARRAY['impression','impression','impression','click','click','conversion'])[1 + floor(random()*6)::int],
    now() - (random()*700 || ' days')::interval
FROM generate_series(1, :n_campaign_events) i;

-- The trap table — a flat 2019-era export, real and queryable, wrong to use.

INSERT INTO analytics.legacy_orders_flat (order_id, customer_name, customer_email, total_amount, order_date, status_code)
SELECT
    o.id, c.name, c.email,
    round((ot.total)::numeric, 0),   -- legacy export rounded to whole currency units — another silent trap
    o.ordered_at::date,
    CASE o.status
        WHEN 'pending' THEN 1 WHEN 'paid' THEN 2 WHEN 'shipped' THEN 2
        WHEN 'delivered' THEN 3 WHEN 'cancelled' THEN 4 WHEN 'refunded' THEN 4
    END
FROM analytics.orders o
JOIN analytics.customers c ON c.id = o.customer_id
JOIN LATERAL (
    SELECT COALESCE(SUM(unit_price * quantity), 0) AS total
    FROM analytics.order_items oi WHERE oi.order_id = o.id
) ot ON true
WHERE o.id <= 50000;

ANALYZE;
