"""Plan budget — layer 3 of the defense stack, and the one layer that was
missing after Phase 03: read-only isn't the same as safe (an unbounded
cross join is valid, read-only SQL that can still take the box down).
Runs EXPLAIN on guard-approved SQL, before execution, and rejects anything
whose estimated cost or row count is wildly out of line with what a real
analytics question over this warehouse should ever need.

Thresholds calibrated against this project's own live database, not
guessed: the heaviest legitimate query in the semantic layer (the
net_revenue CTE, full-table, grouped) costs ~43K and estimates 4 rows; an
unfiltered `SELECT * FROM v_order_items` costs ~19K / ~493K rows; a
deliberate cartesian join (`FROM v_orders, v_order_items` with no ON)
costs ~1.2 BILLION and estimates 97 BILLION rows.

This layer and the guard's row cap (ast_guard._inject_row_cap) cover
different attack shapes, discovered while testing this module rather than
assumed: a bare, unfiltered `SELECT * FROM v_orders, v_order_items` with
the guard's LIMIT 1000 already applied is genuinely CHEAP (~13 cost) —
Postgres can produce the first 1000 rows of an unfiltered cross join with
a handful of nested-loop iterations, so a low cost estimate here is
correct, not a blind spot. The same join wrapped in an aggregate
(`SELECT count(*) FROM v_orders, v_order_items`) costs ~608 MILLION,
because an aggregate must fully materialize its input before producing
any output row — no LIMIT can rescue that. So: LIMIT defends against
"too much data returned"; this layer defends against "too expensive to
compute at all," which is the case a GROUP BY, DISTINCT, ORDER BY, or
aggregate over an unfiltered join actually represents, and the reason
this layer earns its place even though every query it sees already has
a LIMIT on it.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from .execute import ExecutionError, chatbot_database_url

MAX_ESTIMATED_COST = 500_000.0
MAX_ESTIMATED_ROWS = 5_000_000


@dataclass(frozen=True)
class PlanBudgetResult:
    ok: bool
    estimated_cost: float
    estimated_rows: int
    detail: str


def check(safe_sql: str, tenant_id: int, database_url: str | None = None) -> PlanBudgetResult:
    """`safe_sql` must already be guard.check()-approved. EXPLAIN alone
    never executes the query (Postgres plans it but doesn't run it), so
    this is cheap even for a query that would otherwise be expensive.
    """
    dsn = database_url or chatbot_database_url()
    with psycopg.connect(dsn) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
                cur.execute(f"EXPLAIN (FORMAT JSON) {safe_sql}")  # noqa: S608 — safe_sql is guard-approved, not user input
                (plan_json,) = cur.fetchone()
        except psycopg.Error as exc:
            sqlstate = getattr(exc.diag, "sqlstate", None)
            raise ExecutionError(str(exc).strip(), sqlstate=sqlstate) from exc
        finally:
            conn.rollback()

    plan = plan_json[0]["Plan"]
    cost = float(plan["Total Cost"])
    rows = int(plan["Plan Rows"])

    if cost > MAX_ESTIMATED_COST or rows > MAX_ESTIMATED_ROWS:
        return PlanBudgetResult(
            ok=False,
            estimated_cost=cost,
            estimated_rows=rows,
            detail=(
                f"estimated cost {cost:,.0f} (budget {MAX_ESTIMATED_COST:,.0f}) / "
                f"estimated rows {rows:,} (budget {MAX_ESTIMATED_ROWS:,}) — "
                "add a filter (a date range, a status, a LIMIT) to narrow this query"
            ),
        )
    return PlanBudgetResult(ok=True, estimated_cost=cost, estimated_rows=rows, detail="within budget")
