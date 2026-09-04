"""Plan budget — layer 3 of the defense stack: read-only isn't the same
as safe (an unbounded cross join is valid, read-only SQL that can still
take the box down). Runs EXPLAIN on guard-approved SQL before execution
and rejects anything wildly out of line with a real analytics question.

Thresholds calibrated against this project's live database: the
heaviest legit query costs ~43K/4 rows; a deliberate cartesian join
costs ~1.2 billion/97 billion rows.

Covers a different attack shape than the guard's LIMIT injection: a
bare unfiltered cross join with LIMIT 1000 already applied is genuinely
cheap (Postgres stops after a few nested-loop iterations) — the same
join wrapped in an aggregate costs ~608 million, because an aggregate
must fully materialize its input first and no LIMIT can rescue that.
LIMIT defends against too much data returned; this layer defends
against too expensive to compute at all.
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
