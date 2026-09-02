"""The golden set's source of truth. Python, not hand-written YAML,
because multi-line SQL as Python triple-quoted strings is much easier to
read and edit correctly than YAML block scalars — this module is compiled
to eval/golden/{easy,medium,hard}.yml by `python -m eval.golden._cases`
(run from the eval/ directory), which is also where every case gets
re-verified against a live database before being written out. Never hand
-edit the YAML files directly; edit here and rebuild.

Every case was individually run against the live, seeded database at
authoring time — not just written and hoped. One caught a real, previously
undetected data bug in the process: db/01_seed.sql originally assigned
every one of the 2000 products to a single category (the exact
uncorrelated-random-in-a-subquery bug documented at that file's top, in a
spot Phase 00's own verification never specifically checked) — a
category-grouped rating query returning 1 row instead of ~15 was the tell.
Fixed at the source; see db/01_seed.sql's Products section.
"""

from __future__ import annotations

EASY = [
    ("count_all_orders", "How many orders are there in total?",
     "SELECT count(*) FROM analytics.v_orders"),
    ("count_all_customers", "How many customers do we have?",
     "SELECT count(*) FROM analytics.v_customers"),
    ("count_all_products", "How many products are in the catalog?",
     "SELECT count(*) FROM analytics.v_products"),
    ("orders_by_status", "How many orders are there for each status?",
     "SELECT status, count(*) FROM analytics.v_orders GROUP BY status ORDER BY status"),
    ("customers_by_country", "How many customers are in each country?",
     "SELECT country, count(*) FROM analytics.v_customers GROUP BY country ORDER BY country"),
    ("customers_by_region", "Break down customers by region.",
     "SELECT region, count(*) FROM analytics.v_customers GROUP BY region ORDER BY region"),
    ("distinct_statuses", "What are the distinct order statuses?",
     "SELECT DISTINCT status FROM analytics.v_orders ORDER BY status"),
    ("distinct_currencies", "What currencies do we use for orders?",
     "SELECT DISTINCT currency FROM analytics.v_orders ORDER BY currency"),
    ("avg_product_price", "What is the average product price?",
     "SELECT avg(unit_price_usd) FROM analytics.v_products"),
    ("max_product_price", "What is the most expensive product's price?",
     "SELECT max(unit_price_usd) FROM analytics.v_products"),
    ("min_product_price", "What is the cheapest product's price?",
     "SELECT min(unit_price_usd) FROM analytics.v_products"),
    ("tickets_by_status", "How many support tickets are in each status?",
     "SELECT status, count(*) FROM analytics.v_support_tickets GROUP BY status ORDER BY status"),
    ("events_by_type", "How many campaign events are there of each type?",
     "SELECT event_type, count(*) FROM analytics.v_campaign_events GROUP BY event_type ORDER BY event_type"),
    ("count_warehouses", "How many warehouses do we have?",
     "SELECT count(*) FROM analytics.v_warehouses"),
    ("count_categories", "How many product categories are there?",
     "SELECT count(*) FROM analytics.v_categories"),
    ("total_inventory", "What's the total quantity on hand across all inventory?",
     "SELECT sum(qty_on_hand) FROM analytics.v_inventory"),
    ("count_reviews", "How many reviews have been left?",
     "SELECT count(*) FROM analytics.v_reviews"),
    ("avg_rating", "What is the average review rating overall?",
     "SELECT avg(rating) FROM analytics.v_reviews"),
    ("campaigns_by_channel", "How many marketing campaigns per channel?",
     "SELECT channel, count(*) FROM analytics.v_marketing_campaigns GROUP BY channel ORDER BY channel"),
    ("count_shipments", "How many shipments have there been?",
     "SELECT count(*) FROM analytics.v_shipments"),
]

MEDIUM = [
    ("orders_last_30_days", "How many orders were placed in the last 30 days?",
     "SELECT count(*) FROM analytics.v_orders WHERE ordered_at >= now() - interval '30 days'"),
    ("top5_products_by_units", "What are the top 5 products by units sold?",
     """SELECT p.name, sum(oi.quantity) AS units
        FROM analytics.v_order_items oi JOIN analytics.v_products p ON p.id = oi.product_id
        GROUP BY p.name ORDER BY units DESC LIMIT 5"""),
    ("avg_order_value", "What is the average order value?",
     """SELECT avg(order_total) FROM (
          SELECT oi.order_id, sum(oi.quantity * oi.unit_price * (1 - oi.discount_pct/100.0)) AS order_total
          FROM analytics.v_order_items oi GROUP BY oi.order_id
        ) t"""),
    ("customers_over_3_orders", "How many customers have placed more than 3 orders?",
     """SELECT count(*) FROM (
          SELECT customer_id FROM analytics.v_orders GROUP BY customer_id HAVING count(*) > 3
        ) t"""),
    ("products_never_ordered", "How many products have never been ordered?",
     """SELECT count(*) FROM analytics.v_products p
        WHERE NOT EXISTS (SELECT 1 FROM analytics.v_order_items oi WHERE oi.product_id = p.id)"""),
    ("orders_without_payment", "How many orders have no payment on record?",
     """SELECT count(*) FROM analytics.v_orders o
        WHERE NOT EXISTS (SELECT 1 FROM analytics.v_payments p WHERE p.order_id = o.id)"""),
    ("refund_rate", "What fraction of orders have a refund?",
     """SELECT count(DISTINCT r.order_id)::numeric / NULLIF(count(DISTINCT o.id), 0)
        FROM analytics.v_orders o LEFT JOIN analytics.v_refunds r ON r.order_id = o.id"""),
    ("shipments_by_carrier", "How many shipments per carrier?",
     "SELECT carrier, count(*) FROM analytics.v_shipments GROUP BY carrier ORDER BY carrier"),
    ("avg_rating_by_category", "What's the average rating by product category?",
     """SELECT c.name, avg(r.rating) FROM analytics.v_reviews r
        JOIN analytics.v_products p ON p.id = r.product_id
        JOIN analytics.v_categories c ON c.id = p.category_id
        GROUP BY c.name ORDER BY c.name"""),
    ("customers_in_california", "How many customers are in California?",
     "SELECT count(*) FROM analytics.v_customers WHERE state = 'CA'"),
    ("orders_by_currency", "How many orders use each currency?",
     "SELECT currency, count(*) FROM analytics.v_orders GROUP BY currency ORDER BY count(*) DESC"),
    ("low_inventory_count", "How many product/warehouse combinations have fewer than 20 units on hand?",
     "SELECT count(*) FROM analytics.v_inventory WHERE qty_on_hand < 20"),
    ("tickets_last_6_months", "How many support tickets were opened in the last 6 months?",
     "SELECT count(*) FROM analytics.v_support_tickets WHERE opened_at >= now() - interval '6 months'"),
    ("revenue_by_payment_method", "What's the total payment amount by method?",
     "SELECT method, sum(amount) FROM analytics.v_payments WHERE status = 'succeeded' GROUP BY method ORDER BY method"),
    ("top5_customers_by_spend", "Who are the top 5 customers by total amount paid?",
     """SELECT c.name, sum(p.amount) AS total_paid
        FROM analytics.v_payments p
        JOIN analytics.v_orders o ON o.id = p.order_id
        JOIN analytics.v_customers c ON c.id = o.customer_id
        WHERE p.status = 'succeeded'
        GROUP BY c.name ORDER BY total_paid DESC LIMIT 5"""),
    ("avg_shipping_lag", "What's the average number of days between an order being placed and delivered?",
     """SELECT avg(extract(epoch FROM (s.delivered_at - o.ordered_at)) / 86400.0)
        FROM analytics.v_shipments s JOIN analytics.v_orders o ON o.id = s.order_id
        WHERE s.delivered_at IS NOT NULL"""),
    ("customers_null_state", "How many customers have no state on file?",
     "SELECT count(*) FROM analytics.v_customers WHERE state IS NULL"),
    ("products_out_of_stock_somewhere", "How many products are out of stock in at least one warehouse?",
     "SELECT count(DISTINCT product_id) FROM analytics.v_inventory WHERE qty_on_hand = 0"),
    ("open_tickets_with_order", "How many open support tickets are linked to an order?",
     "SELECT count(*) FROM analytics.v_support_tickets WHERE status = 'open' AND order_id IS NOT NULL"),
    ("distinct_carriers", "Which carriers do we use for shipping?",
     "SELECT DISTINCT carrier FROM analytics.v_shipments ORDER BY carrier"),
]

HARD = [
    ("net_revenue_by_region", "What is our net revenue by region?",
     """WITH order_totals AS (
          SELECT oi.order_id, sum(oi.quantity * oi.unit_price * (1 - oi.discount_pct/100.0)) AS gross
          FROM analytics.v_order_items oi GROUP BY oi.order_id
        ), order_refunds AS (
          SELECT r.order_id, sum(r.amount) AS refunded FROM analytics.v_refunds r GROUP BY r.order_id
        )
        SELECT c.region, sum(ot.gross - COALESCE(orf.refunded, 0)) AS net_revenue
        FROM analytics.v_orders o
        JOIN order_totals ot ON ot.order_id = o.id
        LEFT JOIN order_refunds orf ON orf.order_id = o.id
        JOIN analytics.v_customers c ON c.id = o.customer_id
        GROUP BY c.region ORDER BY net_revenue DESC"""),
    ("top5_products_net_revenue", "What are the top 5 products by net revenue after refunds?",
     """WITH item_gross AS (
          SELECT oi.order_id, oi.product_id, oi.quantity * oi.unit_price * (1 - oi.discount_pct/100.0) AS gross
          FROM analytics.v_order_items oi
        ), order_refund_ratio AS (
          SELECT o.id AS order_id,
                 COALESCE(r.refunded, 0) / NULLIF(ot.gross_total, 0) AS refund_frac
          FROM analytics.v_orders o
          JOIN (SELECT order_id, sum(quantity*unit_price*(1-discount_pct/100.0)) AS gross_total
                FROM analytics.v_order_items GROUP BY order_id) ot ON ot.order_id = o.id
          LEFT JOIN (SELECT order_id, sum(amount) AS refunded FROM analytics.v_refunds GROUP BY order_id) r
                 ON r.order_id = o.id
        )
        SELECT p.name, sum(ig.gross * (1 - COALESCE(rr.refund_frac, 0))) AS net_revenue
        FROM item_gross ig
        JOIN analytics.v_products p ON p.id = ig.product_id
        JOIN order_refund_ratio rr ON rr.order_id = ig.order_id
        GROUP BY p.name ORDER BY net_revenue DESC LIMIT 5"""),
    ("net_revenue_last_year_by_month", "What was net revenue by month over the last year?",
     """WITH order_totals AS (
          SELECT oi.order_id, sum(oi.quantity * oi.unit_price * (1 - oi.discount_pct/100.0)) AS gross
          FROM analytics.v_order_items oi GROUP BY oi.order_id
        ), order_refunds AS (
          SELECT r.order_id, sum(r.amount) AS refunded FROM analytics.v_refunds r GROUP BY r.order_id
        )
        SELECT date_trunc('month', o.ordered_at) AS month, sum(ot.gross - COALESCE(orf.refunded,0)) AS net_revenue
        FROM analytics.v_orders o
        JOIN order_totals ot ON ot.order_id = o.id
        LEFT JOIN order_refunds orf ON orf.order_id = o.id
        WHERE o.ordered_at >= now() - interval '1 year'
        GROUP BY month ORDER BY month"""),
    ("customers_never_reviewed", "How many customers have never left a review?",
     """SELECT count(*) FROM analytics.v_customers c
        WHERE NOT EXISTS (SELECT 1 FROM analytics.v_reviews r WHERE r.customer_id = c.id)"""),
    ("products_never_reviewed", "How many products have never been reviewed?",
     """SELECT count(*) FROM analytics.v_products p
        WHERE NOT EXISTS (SELECT 1 FROM analytics.v_reviews r WHERE r.product_id = p.id)"""),
    ("revenue_created_vs_ordered_trap", "What was our net revenue last month?",
     """WITH order_totals AS (
          SELECT oi.order_id, sum(oi.quantity * oi.unit_price * (1 - oi.discount_pct/100.0)) AS gross
          FROM analytics.v_order_items oi GROUP BY oi.order_id
        ), order_refunds AS (
          SELECT r.order_id, sum(r.amount) AS refunded FROM analytics.v_refunds r GROUP BY r.order_id
        )
        SELECT sum(ot.gross - COALESCE(orf.refunded, 0))
        FROM analytics.v_orders o
        JOIN order_totals ot ON ot.order_id = o.id
        LEFT JOIN order_refunds orf ON orf.order_id = o.id
        WHERE o.ordered_at >= date_trunc('month', now()) - interval '1 month'
          AND o.ordered_at < date_trunc('month', now())"""),
    ("refund_rate_by_category", "What's the refund rate by product category?",
     """WITH order_has_refund AS (
          SELECT DISTINCT order_id FROM analytics.v_refunds
        )
        SELECT c.name,
               count(DISTINCT oi.order_id) FILTER (WHERE ohr.order_id IS NOT NULL)::numeric
                 / NULLIF(count(DISTINCT oi.order_id), 0) AS refund_rate
        FROM analytics.v_order_items oi
        JOIN analytics.v_products p ON p.id = oi.product_id
        JOIN analytics.v_categories c ON c.id = p.category_id
        LEFT JOIN order_has_refund ohr ON ohr.order_id = oi.order_id
        GROUP BY c.name ORDER BY c.name"""),
    ("mom_order_count_change", "How did order count change from two months ago to last month?",
     """WITH monthly AS (
          SELECT date_trunc('month', ordered_at) AS month, count(*) AS n
          FROM analytics.v_orders
          WHERE ordered_at >= date_trunc('month', now()) - interval '2 months'
            AND ordered_at < date_trunc('month', now())
          GROUP BY month
        )
        SELECT month, n, n - lag(n) OVER (ORDER BY month) AS delta
        FROM monthly ORDER BY month"""),
    ("campaign_conversion_rate", "What's the conversion rate for each marketing campaign?",
     """SELECT mc.name,
               count(*) FILTER (WHERE ce.event_type = 'conversion')::numeric
                 / NULLIF(count(*) FILTER (WHERE ce.event_type = 'impression'), 0) AS conversion_rate
        FROM analytics.v_campaign_events ce
        JOIN analytics.v_marketing_campaigns mc ON mc.id = ce.campaign_id
        GROUP BY mc.name ORDER BY mc.name"""),
    ("customers_first_order_over_threshold", "How many customers' first order was over $500?",
     """WITH first_orders AS (
          SELECT DISTINCT ON (o.customer_id) o.customer_id, o.id AS order_id
          FROM analytics.v_orders o ORDER BY o.customer_id, o.ordered_at ASC
        ), first_order_totals AS (
          SELECT fo.customer_id, sum(oi.quantity * oi.unit_price * (1 - oi.discount_pct/100.0)) AS total
          FROM first_orders fo
          JOIN analytics.v_order_items oi ON oi.order_id = fo.order_id
          GROUP BY fo.customer_id
        )
        SELECT count(*) FROM first_order_totals WHERE total > 500"""),
    ("avg_discount_refunded_vs_not", "Is the average discount percentage different on orders that were refunded versus not?",
     """SELECT (EXISTS (SELECT 1 FROM analytics.v_refunds r WHERE r.order_id = oi.order_id)) AS was_refunded,
               avg(oi.discount_pct)
        FROM analytics.v_order_items oi
        GROUP BY was_refunded ORDER BY was_refunded"""),
    ("inventory_value_by_warehouse", "What's the total inventory value (quantity times unit price) by warehouse?",
     """SELECT w.name, sum(i.qty_on_hand * p.unit_price_usd) AS inventory_value
        FROM analytics.v_inventory i
        JOIN analytics.v_warehouses w ON w.id = i.warehouse_id
        JOIN analytics.v_products p ON p.id = i.product_id
        GROUP BY w.name ORDER BY inventory_value DESC"""),
]

ALL_TIERS = {"easy": EASY, "medium": MEDIUM, "hard": HARD}
