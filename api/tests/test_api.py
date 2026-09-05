"""API-level tests for the SSE endpoints and the approval gate. Stubs
only the LLM call (via monkeypatching app.pipeline.answer.generate_plan,
same technique as test_answer_repair_loop.py) — the guard, budget check,
and execution are all real against the live database. This is what
proves the approval-gate PATH itself (pause on medium confidence,
resume on approve, discard on reject) without depending on this
project's local model ever actually saying "medium" (checked against
real history: it said "high" 19 times out of 20 real questions and
"low" once, paired with a clarifying question — see README's Phase 06
section. The mechanism is real and tested; the trigger rarely fires
with this specific model).
"""

from __future__ import annotations

import json

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.llm.client import GenerationResult as _GenerationResult
from app.llm.schemas import SqlPlan
from app.pipeline import answer as answer_module

from .conftest import TEST_DATABASE_URL, requires_db  # noqa: F401
from .conftest import operator_headers, operator_user  # noqa: F401


def _gen_result(sql: str, confidence: str = "high") -> _GenerationResult:
    plan = SqlPlan(intent="test question", sql=sql, confidence=confidence)
    return _GenerationResult(plan=plan, model="fake", prompt_eval_count=1, eval_count=1, total_duration_ms=1.0)


def _stub_generate_plan(monkeypatch, *results):
    calls = {"n": 0}

    def fake(ctx, question, repair_attempts=None, model=None):
        i = min(calls["n"], len(results) - 1)
        calls["n"] += 1
        return results[i]

    monkeypatch.setattr(answer_module, "generate_plan", fake)
    return calls


def _iter_sse_events(text: str):
    """Minimal SSE parser for test assertions — pairs each `event:` line
    with the JSON on the following `data:` line.
    """
    events = []
    lines = text.strip("\n").split("\n")
    i = 0
    while i < len(lines):
        if lines[i].startswith("event:"):
            kind = lines[i][len("event:"):].strip()
            data = json.loads(lines[i + 1][len("data:"):].strip())
            events.append((kind, data))
            i += 2
        else:
            i += 1
    return events


@pytest.fixture()
def client(operator_headers):
    # app.main reads DATABASE_URL at import time and TEST_DATABASE_URL
    # (conftest.py) is the same default — no env patching needed as long
    # as that stays true; asserted here so a future drift fails loudly
    # instead of silently pointing tests at the wrong database.
    from app.main import DATABASE_URL, app

    assert DATABASE_URL == TEST_DATABASE_URL, "app.main's DB default drifted from the test default"

    # Every route below auth requires a bearer token. Attaching it to the
    # client keeps the existing tests about what they were about; the
    # unauthenticated and wrong-role paths get their own tests in
    # test_auth.py rather than being smuggled in here.
    with TestClient(app, headers=operator_headers) as c:
        yield c


@requires_db
def test_health_endpoint(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["schema_fingerprint"]


@requires_db
def test_high_confidence_auto_executes_in_one_stream(client, monkeypatch):
    _stub_generate_plan(monkeypatch, _gen_result("SELECT count(*) FROM analytics.v_orders", confidence="high"))
    resp = client.post("/api/query", json={"question": "how many orders"})
    assert resp.status_code == 200
    events = _iter_sse_events(resp.text)
    kinds = [k for k, _ in events]
    assert "awaiting_approval" not in kinds
    assert kinds[-1] == "done"
    done_data = events[-1][1]
    assert done_data["verdict"] == "answered"
    assert done_data["execution"]["row_count"] == 1


@requires_db
def test_medium_confidence_pauses_for_approval(client, monkeypatch):
    _stub_generate_plan(monkeypatch, _gen_result("SELECT count(*) FROM analytics.v_orders", confidence="medium"))
    resp = client.post("/api/query", json={"question": "how many orders"})
    events = _iter_sse_events(resp.text)
    kinds = [k for k, _ in events]
    assert kinds[-1] == "awaiting_approval"
    assert "done" not in kinds  # never executed without approval
    payload = events[-1][1]
    assert payload["plan_id"]
    assert payload["safe_sql"] == "SELECT count(*) FROM analytics.v_orders LIMIT 1000"


@requires_db
def test_approving_a_paused_plan_executes_it(client, monkeypatch):
    _stub_generate_plan(monkeypatch, _gen_result("SELECT count(*) FROM analytics.v_orders", confidence="low"))
    paused = client.post("/api/query", json={"question": "how many orders"})
    plan_id = _iter_sse_events(paused.text)[-1][1]["plan_id"]

    resp = client.post("/api/query/approve", json={"plan_id": plan_id})
    events = _iter_sse_events(resp.text)
    done = events[-1][1]
    assert done["verdict"] == "answered"
    assert done["execution"]["row_count"] == 1


@requires_db
def test_approving_twice_fails_the_second_time(client, monkeypatch):
    # A plan_id is consumed on approval — resubmitting the same id (double
    # -click, a retried request) must not re-execute it a second time.
    _stub_generate_plan(monkeypatch, _gen_result("SELECT count(*) FROM analytics.v_orders", confidence="low"))
    paused = client.post("/api/query", json={"question": "how many orders"})
    plan_id = _iter_sse_events(paused.text)[-1][1]["plan_id"]

    client.post("/api/query/approve", json={"plan_id": plan_id})
    second = client.post("/api/query/approve", json={"plan_id": plan_id})
    assert second.status_code == 404


@requires_db
def test_edited_sql_is_reguarded_not_trusted(client, monkeypatch):
    # Editing the SQL in the query card is not a privileged path — a
    # human-supplied DROP TABLE must be caught exactly like model output.
    _stub_generate_plan(monkeypatch, _gen_result("SELECT count(*) FROM analytics.v_orders", confidence="low"))
    paused = client.post("/api/query", json={"question": "how many orders"})
    plan_id = _iter_sse_events(paused.text)[-1][1]["plan_id"]

    resp = client.post("/api/query/approve", json={"plan_id": plan_id, "sql": "DROP TABLE analytics.orders"})
    events = _iter_sse_events(resp.text)
    done = events[-1][1]
    assert done["verdict"] == "block"


@requires_db
def test_edited_sql_that_is_valid_executes_the_edited_version(client, monkeypatch):
    _stub_generate_plan(monkeypatch, _gen_result("SELECT count(*) FROM analytics.v_orders", confidence="low"))
    paused = client.post("/api/query", json={"question": "how many orders"})
    plan_id = _iter_sse_events(paused.text)[-1][1]["plan_id"]

    resp = client.post("/api/query/approve", json={
        "plan_id": plan_id,
        "sql": "SELECT count(*) FROM analytics.v_customers",  # human changed the table
    })
    events = _iter_sse_events(resp.text)
    done = events[-1][1]
    assert done["verdict"] == "answered"
    # v_customers count differs from v_orders count — confirms the EDITED
    # sql ran, not the original plan's sql.
    assert done["last_sql"].strip().startswith("SELECT count(*) FROM analytics.v_customers")


@requires_db
def test_rejecting_a_plan_discards_it(client, monkeypatch):
    _stub_generate_plan(monkeypatch, _gen_result("SELECT count(*) FROM analytics.v_orders", confidence="low"))
    paused = client.post("/api/query", json={"question": "how many orders"})
    plan_id = _iter_sse_events(paused.text)[-1][1]["plan_id"]

    reject_resp = client.post(f"/api/query/{plan_id}/reject")
    assert reject_resp.status_code == 200

    approve_resp = client.post("/api/query/approve", json={"plan_id": plan_id})
    assert approve_resp.status_code == 404


@requires_db
def test_ambiguous_question_streams_ask_verdict_no_approval_needed(client, monkeypatch):
    plan = SqlPlan(
        intent="ambiguous", sql="SELECT 1 WHERE false", confidence="low",
        clarifying_question="Which metric do you mean?",
    )
    gen = _GenerationResult(plan=plan, model="fake", prompt_eval_count=1, eval_count=1, total_duration_ms=1.0)
    _stub_generate_plan(monkeypatch, gen)

    resp = client.post("/api/query", json={"question": "how did we do"})
    events = _iter_sse_events(resp.text)
    kinds = [k for k, _ in events]
    assert "awaiting_approval" not in kinds
    assert events[-1][0] == "done"
    assert events[-1][1]["verdict"] == "ask"


# ---------------------------------------------------------------------------
# Observability — audit logging + the dashboard endpoints (Phase 07)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_audit_log_for_api_tests():
    with psycopg.connect(TEST_DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE audit.query_log RESTART IDENTITY")
        conn.commit()
    yield
    with psycopg.connect(TEST_DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE audit.query_log RESTART IDENTITY")
        conn.commit()


@requires_db
def test_answered_query_is_audit_logged(client, monkeypatch):
    _stub_generate_plan(monkeypatch, _gen_result("SELECT count(*) FROM analytics.v_orders", confidence="high"))
    client.post("/api/query", json={"question": "how many orders"})

    stats_resp = client.get("/api/stats")
    assert stats_resp.status_code == 200
    body = stats_resp.json()
    assert body["total_queries"] == 1
    assert any(v["verdict"] == "answered" and v["count"] == 1 for v in body["by_verdict"])


@requires_db
def test_blocked_query_shows_up_in_the_audit_view(client, monkeypatch):
    _stub_generate_plan(monkeypatch, _gen_result("DROP TABLE analytics.orders", confidence="high"))
    client.post("/api/query", json={"question": "delete everything"})

    audit_resp = client.get("/api/audit")
    assert audit_resp.status_code == 200
    events = audit_resp.json()["events"]
    assert len(events) == 1
    assert events[0]["verdict"] == "block"
    assert events[0]["generated_sql"] == "DROP TABLE analytics.orders"


@requires_db
def test_answered_query_does_not_appear_in_the_default_audit_view(client, monkeypatch):
    # /api/audit defaults to block/give_up — "the interesting ones" — a
    # routine answered question shouldn't clutter it.
    _stub_generate_plan(monkeypatch, _gen_result("SELECT count(*) FROM analytics.v_orders", confidence="high"))
    client.post("/api/query", json={"question": "how many orders"})

    events = client.get("/api/audit").json()["events"]
    assert events == []


@requires_db
def test_edited_and_approved_query_is_logged_as_edited(client, monkeypatch):
    _stub_generate_plan(monkeypatch, _gen_result("SELECT count(*) FROM analytics.v_orders", confidence="low"))
    paused = client.post("/api/query", json={"question": "how many orders"})
    plan_id = _iter_sse_events(paused.text)[-1][1]["plan_id"]

    client.post("/api/query/approve", json={
        "plan_id": plan_id, "sql": "SELECT count(*) FROM analytics.v_customers",
    })

    with psycopg.connect(TEST_DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("SELECT edited, safe_sql FROM audit.query_log")
        row = cur.fetchone()
    assert row[0] is True
    assert "v_customers" in row[1]
