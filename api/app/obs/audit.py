"""Writes audit.query_log — one row per question, whatever the verdict.
Uses the app's own trusted connection, never chatbot_ro (zero grants on
the audit schema by design — db/02_roles.sql).

Scoped to the API surface deliberately, not the CLI or eval harness:
this table is the running service's real activity trail, and test runs
would drown it in noise. See api/app/api/routes.py for the call sites.
"""

from __future__ import annotations

import os
import uuid

import psycopg

from ..pipeline.answer import AnswerOutcome


def app_database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/querywarden")


def log_query_run(
    outcome: AnswerOutcome,
    tenant_id: int,
    model: str,
    request_id: uuid.UUID | None = None,
    edited: bool = False,
    database_url: str | None = None,
) -> uuid.UUID:
    """Never silent here — a write failure raises; the caller (routes.py)
    decides whether to swallow it."""
    request_id = request_id or uuid.uuid4()
    plan = outcome.plan
    execution = outcome.execution

    with psycopg.connect(database_url or app_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit.query_log
                    (request_id, tenant_id, question, model, verdict, failure_kind,
                     generated_sql, safe_sql, edited, repair_attempts, row_count, duration_ms, message)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(request_id),
                    tenant_id,
                    outcome.question,
                    model,
                    outcome.verdict.value,
                    outcome.failure_kind.value if outcome.failure_kind else None,
                    plan.sql if plan else None,
                    outcome.last_sql,
                    edited,
                    outcome.repair_attempts_used,
                    execution.row_count if execution else None,
                    execution.duration_ms if execution else None,
                    outcome.message,
                ),
            )
        conn.commit()
    return request_id
