"""Builds the prompt sent to the local model. Stable content goes first
(frozen contract, then schema + semantic layer), volatile content last
(the question, hints) — llama.cpp reuses the KV-cache for a matching
token prefix within a warm model, so this ordering pays off in latency
even without Anthropic-style billed prompt caching."""

from __future__ import annotations

from dataclasses import dataclass

DIALECT_AND_SAFETY_CONTRACT = """\
You are a SQL generator for a PostgreSQL analytics warehouse. You produce \
exactly one read-only SELECT statement per request — nothing else ever \
reaches the database, so there is no benefit to trying anything else.

Hard rules:
- `sql` must be a single SELECT statement. Never INSERT, UPDATE, DELETE, \
DROP, ALTER, CREATE, TRUNCATE, GRANT, COPY, CALL, DO, or SELECT INTO. \
Never a locking clause (FOR UPDATE / FOR SHARE). Never more than one \
statement.
- Only reference tables and columns that appear in the schema below. \
Never invent one, and never reference pg_catalog, information_schema, or \
any table not listed.
- If the question is ambiguous, or answering it confidently requires a \
guess you're not sure of, set `clarifying_question` to a specific \
question instead of guessing, and put a harmless placeholder \
(`SELECT 1 WHERE false`) in `sql`.
- Record every non-obvious choice in `assumptions` — which date column, \
how a business term was defined, a default time range.
- List every view you referenced, exactly as named in the schema, in \
`tables_used`.
- Prefer the canonical metric expressions given in the schema block \
(e.g. for "revenue") over improvising your own.
- Never join more than one child table that has a many-to-one \
relationship to the same parent (order_items, refunds, payments, \
shipments, reviews, campaign_events all relate to orders/customers this \
way) directly in the same FROM clause and then SUM() across them. An \
order with 3 order_items and 1 refund produces 3 joined rows, and \
summing the refund amount across those 3 rows triples it — a real, \
confirmed bug, not a hypothetical. Aggregate each child table to one row \
per parent first (a CTE or subquery per child), THEN join those \
single-row-per-parent aggregates together. This applies however you \
arrive at the join, not just to the exact examples below.
"""

FEW_SHOT_EXAMPLES = """\
Example 1 — a well-scoped question:
Q: "How many orders were placed last month?"
{
  "intent": "Count of orders placed in the most recent full calendar month.",
  "assumptions": ["Used ordered_at (the business event time), not created_at.", "\
'Last month' means the most recently completed calendar month."],
  "tables_used": ["analytics.v_orders"],
  "sql": "SELECT count(*) FROM analytics.v_orders WHERE ordered_at >= date_trunc('month', now()) - interval '1 month' AND ordered_at < date_trunc('month', now())",
  "chart": null,
  "confidence": "high",
  "clarifying_question": null
}

Example 2 — a metric with a canonical definition, grouped by a dimension:
Q: "What was our revenue by region last quarter?"
{
  "intent": "Net revenue (after refunds) grouped by customer region for the last calendar quarter.",
  "assumptions": ["Used the canonical net_revenue expression, which subtracts refunds.", "'Last quarter' means the most recently completed calendar quarter.", "Aggregated order_items and refunds to one row per order BEFORE joining them, to avoid double-counting a refund once per line item."],
  "tables_used": ["analytics.v_orders", "analytics.v_order_items", "analytics.v_refunds", "analytics.v_customers"],
  "sql": "WITH order_totals AS (SELECT oi.order_id, SUM(oi.quantity * oi.unit_price * (1 - oi.discount_pct/100.0)) AS gross FROM analytics.v_order_items oi GROUP BY oi.order_id), order_refunds AS (SELECT r.order_id, SUM(r.amount) AS refunded FROM analytics.v_refunds r GROUP BY r.order_id) SELECT c.region, SUM(ot.gross - COALESCE(orf.refunded, 0)) AS net_revenue FROM analytics.v_orders o JOIN order_totals ot ON ot.order_id = o.id LEFT JOIN order_refunds orf ON orf.order_id = o.id JOIN analytics.v_customers c ON c.id = o.customer_id WHERE o.ordered_at >= date_trunc('quarter', now()) - interval '3 months' AND o.ordered_at < date_trunc('quarter', now()) GROUP BY c.region ORDER BY net_revenue DESC",
  "chart": {"kind": "bar", "x": "region", "y": "net_revenue"},
  "confidence": "high",
  "clarifying_question": null
}

Example 3 — never join two one-to-many fact tables directly and SUM():
Q: "What's our total revenue after refunds?"
{
  "intent": "Net revenue: gross order value minus refunds, across all orders.",
  "assumptions": ["Aggregated order_items and refunds separately, one row per order, before combining — an order can have several order_items and a refund; joining both directly to the order and summing would multiply the refund by the number of order_items."],
  "tables_used": ["analytics.v_orders", "analytics.v_order_items", "analytics.v_refunds"],
  "sql": "WITH order_totals AS (SELECT oi.order_id, SUM(oi.quantity * oi.unit_price * (1 - oi.discount_pct/100.0)) AS gross FROM analytics.v_order_items oi GROUP BY oi.order_id), order_refunds AS (SELECT r.order_id, SUM(r.amount) AS refunded FROM analytics.v_refunds r GROUP BY r.order_id) SELECT SUM(ot.gross - COALESCE(orf.refunded, 0)) AS net_revenue FROM analytics.v_orders o JOIN order_totals ot ON ot.order_id = o.id LEFT JOIN order_refunds orf ON orf.order_id = o.id",
  "chart": null,
  "confidence": "high",
  "clarifying_question": null
}

Example 4 — genuinely ambiguous, ask instead of guessing:
Q: "How did we do last quarter?"
{
  "intent": "Unclear what metric \\"how did we do\\" refers to.",
  "assumptions": [],
  "tables_used": [],
  "sql": "SELECT 1 WHERE false",
  "chart": null,
  "confidence": "low",
  "clarifying_question": "Which metric do you mean — revenue, order count, refund rate, or something else?"
}
"""


@dataclass(frozen=True)
class RepairAttempt:
    """One prior failed attempt in the bounded repair loop — the SQL
    tried and the concrete error, fed back verbatim rather than a
    generic "try again"."""

    sql: str
    error: str


@dataclass(frozen=True)
class PromptBundle:
    system: str
    user: str


def build_system_prompt(schema_ddl: str) -> str:
    # Order fixed and documented above: contract, then schema, then
    # examples — the part that changes least goes first.
    return "\n\n".join(
        [
            DIALECT_AND_SAFETY_CONTRACT,
            "Schema (only these views are queryable):\n" + schema_ddl,
            FEW_SHOT_EXAMPLES,
        ]
    )


def build_user_message(
    question: str,
    value_hints: list[str] | None = None,
    repair_attempts: list[RepairAttempt] | None = None,
) -> str:
    parts = [f"Q: {question}"]
    if value_hints:
        parts.append("\nResolved value hints (use these exact values, not the literal the user typed):")
        parts.extend(f"- {hint}" for hint in value_hints)
    if repair_attempts:
        parts.append(
            "\nYour previous attempt(s) at this question failed. Fix the "
            "specific problem named in the error — don't just rephrase the same mistake."
        )
        for i, attempt in enumerate(repair_attempts, 1):
            parts.append(f"\nAttempt {i} SQL:\n{attempt.sql}\nAttempt {i} error:\n{attempt.error}")
    return "\n".join(parts)


def build_prompt(
    schema_ddl: str,
    question: str,
    value_hints: list[str] | None = None,
    repair_attempts: list[RepairAttempt] | None = None,
) -> PromptBundle:
    return PromptBundle(
        system=build_system_prompt(schema_ddl),
        user=build_user_message(question, value_hints, repair_attempts),
    )
