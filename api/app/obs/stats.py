"""Aggregate queries over audit.query_log for the cost/latency dashboard
and the audit-log view — read-only, same trusted connection as the
writer (audit.py). Kept as plain SQL aggregates rather than an ORM: this
is two small, fixed queries, not a general reporting layer.
"""

from __future__ import annotations

import psycopg

from .audit import app_database_url


def get_summary(hours: int = 24, database_url: str | None = None) -> dict:
    with psycopg.connect(database_url or app_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    verdict,
                    count(*),
                    avg(duration_ms) FILTER (WHERE duration_ms IS NOT NULL),
                    avg(repair_attempts)
                FROM audit.query_log
                WHERE occurred_at >= now() - (%s || ' hours')::interval
                GROUP BY verdict
                ORDER BY verdict
                """,
                (hours,),
            )
            by_verdict = [
                {
                    "verdict": row[0],
                    "count": row[1],
                    "avg_duration_ms": round(row[2], 1) if row[2] is not None else None,
                    "avg_repair_attempts": round(row[3], 2) if row[3] is not None else None,
                }
                for row in cur.fetchall()
            ]

            cur.execute(
                """
                SELECT count(*), avg(duration_ms) FILTER (WHERE duration_ms IS NOT NULL)
                FROM audit.query_log
                WHERE occurred_at >= now() - (%s || ' hours')::interval
                """,
                (hours,),
            )
            total, avg_duration = cur.fetchone()

    return {
        "window_hours": hours,
        "total_queries": total or 0,
        "avg_duration_ms": round(avg_duration, 1) if avg_duration is not None else None,
        "by_verdict": by_verdict,
    }


def get_recent_events(verdicts: tuple[str, ...] = ("block", "give_up"), limit: int = 50, database_url: str | None = None) -> list[dict]:
    """The audit-log view: blocked and given-up attempts by default —
    "the interesting ones" per the design doc — not the full firehose.
    """
    with psycopg.connect(database_url or app_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT request_id, occurred_at, tenant_id, question, verdict, failure_kind,
                       generated_sql, safe_sql, edited, repair_attempts, message
                FROM audit.query_log
                WHERE verdict = ANY(%s)
                ORDER BY occurred_at DESC
                LIMIT %s
                """,
                (list(verdicts), limit),
            )
            columns = [d.name for d in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
