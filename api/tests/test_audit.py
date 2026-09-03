"""Audit logging and the dashboard queries — against a live database,
using the app's own trusted connection exactly like the real code does.
Includes the permission check as a real, executable regression test
(not just proven once by hand at the psql prompt): chatbot_ro must never
be able to read or write audit.query_log, whatever else changes.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from app.llm.schemas import SqlPlan
from app.obs.audit import log_query_run
from app.obs.stats import get_recent_events, get_summary
from app.pipeline.answer import AnswerOutcome
from app.pipeline.errors import Action, FailureKind
from app.pipeline.execute import ExecutionResult

from .conftest import TEST_DATABASE_URL, requires_db


def _answered_outcome(question: str = "how many orders") -> AnswerOutcome:
    plan = SqlPlan(intent="test", sql="SELECT count(*) FROM analytics.v_orders", confidence="high")
    execution = ExecutionResult(columns=["count"], rows=[(42,)], row_count=1, duration_ms=12.5)
    return AnswerOutcome(
        Action.ANSWERED, question, plan, execution, None, "answered",
        repair_attempts_used=0, last_sql="SELECT count(*) FROM analytics.v_orders LIMIT 1000",
        failure_kind=None,
    )


def _blocked_outcome(question: str = "delete everything") -> AnswerOutcome:
    plan = SqlPlan(intent="test", sql="DROP TABLE analytics.orders", confidence="high")
    return AnswerOutcome(
        Action.BLOCK, question, plan, None, None, "statement type not permitted",
        repair_attempts_used=0, last_sql="DROP TABLE analytics.orders",
        failure_kind=FailureKind.UNSAFE,
    )


@pytest.fixture(autouse=True)
def _clean_audit_log():
    # Each test starts and ends with a clean slate so row-count
    # assertions aren't sensitive to test execution order.
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE audit.query_log RESTART IDENTITY")
        conn.commit()
    yield
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE audit.query_log RESTART IDENTITY")
        conn.commit()


@requires_db
def test_log_query_run_writes_an_answered_row():
    request_id = log_query_run(_answered_outcome(), tenant_id=1, model="qwen2.5-coder:3b")
    with psycopg.connect(TEST_DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("SELECT verdict, question, row_count, duration_ms, request_id FROM audit.query_log")
        row = cur.fetchone()
    assert row[0] == "answered"
    assert row[1] == "how many orders"
    assert row[2] == 1
    assert row[3] == pytest.approx(12.5)
    assert uuid.UUID(str(row[4])) == request_id


@requires_db
def test_log_query_run_captures_the_generated_sql_on_a_block():
    # The model's OWN sql is what matters for a blocked attempt — it
    # never got a safe_sql from the guard, by definition.
    log_query_run(_blocked_outcome(), tenant_id=1, model="qwen2.5-coder:3b")
    with psycopg.connect(TEST_DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("SELECT verdict, generated_sql, failure_kind FROM audit.query_log")
        row = cur.fetchone()
    assert row[0] == "block"
    assert row[1] == "DROP TABLE analytics.orders"
    assert row[2] == "unsafe"


@requires_db
def test_get_summary_aggregates_by_verdict():
    log_query_run(_answered_outcome("q1"), tenant_id=1, model="m")
    log_query_run(_answered_outcome("q2"), tenant_id=1, model="m")
    log_query_run(_blocked_outcome("q3"), tenant_id=1, model="m")

    summary = get_summary(hours=24)
    assert summary["total_queries"] == 3
    by_verdict = {row["verdict"]: row["count"] for row in summary["by_verdict"]}
    assert by_verdict["answered"] == 2
    assert by_verdict["block"] == 1


@requires_db
def test_get_recent_events_defaults_to_blocked_and_given_up():
    log_query_run(_answered_outcome(), tenant_id=1, model="m")
    log_query_run(_blocked_outcome(), tenant_id=1, model="m")

    events = get_recent_events()
    assert len(events) == 1
    assert events[0]["verdict"] == "block"


@requires_db
def test_get_recent_events_respects_limit():
    for i in range(5):
        log_query_run(_blocked_outcome(f"attempt {i}"), tenant_id=1, model="m")
    events = get_recent_events(limit=2)
    assert len(events) == 2


@requires_db
def test_chatbot_ro_has_zero_access_to_the_audit_schema():
    # The permission claim from db/02_roles.sql, proven as a real,
    # executable test rather than only checked once by hand at psql.
    dsn = TEST_DATABASE_URL.replace("postgres:postgres@", "chatbot_ro:chatbot_ro@")
    with psycopg.connect(dsn) as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT * FROM audit.query_log")
        conn.rollback()
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "INSERT INTO audit.query_log (request_id, tenant_id, question, model, verdict, message) "
                    "VALUES (gen_random_uuid(), 1, 'x', 'x', 'answered', 'x')"
                )
        conn.rollback()
