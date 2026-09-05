"""Conversation history: persistence, and the ownership checks that stop
a conversation id from being enough to read someone else's thread."""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from .conftest import (  # noqa: F401
    TEST_DATABASE_URL,
    TEST_PASSWORD,
    auth_headers_for,
    member_headers,
    member_user,
    operator_headers,
    operator_user,
    requires_db,
)


@pytest.fixture()
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def _new_conversation(client, headers, title=None) -> str:
    resp = client.post("/api/conversations", headers=headers, json={"title": title})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@requires_db
def test_a_new_conversation_starts_empty(client, operator_headers):
    convo_id = _new_conversation(client, operator_headers)
    resp = client.get(f"/api/conversations/{convo_id}", headers=operator_headers)
    assert resp.status_code == 200
    assert resp.json()["messages"] == []


@requires_db
def test_a_turn_is_persisted_and_retitles_the_conversation(client, operator_headers, operator_user):
    from app.history import store

    convo_id = _new_conversation(client, operator_headers)
    ok = store.append_turn(
        convo_id, operator_user.id, operator_user.tenant_id,
        question="How many orders were shipped?",
        outcome={"verdict": "answered", "message": "answered", "execution": {"row_count": 1}},
        database_url=TEST_DATABASE_URL,
    )
    assert ok

    messages = client.get(f"/api/conversations/{convo_id}", headers=operator_headers).json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "How many orders were shipped?"
    # The whole outcome round-trips, so reloading replays what was seen
    # rather than re-running the query against changed data.
    assert messages[1]["outcome"]["verdict"] == "answered"

    listing = client.get("/api/conversations", headers=operator_headers).json()["conversations"]
    titled = next(c for c in listing if c["id"] == convo_id)
    assert titled["title"] == "How many orders were shipped?"
    assert titled["message_count"] == 2


@requires_db
def test_a_long_question_is_elided_in_the_title(client, operator_headers, operator_user):
    from app.history import store

    convo_id = _new_conversation(client, operator_headers)
    long_q = "What is the average " + "very " * 40 + "long question?"
    store.append_turn(
        convo_id, operator_user.id, operator_user.tenant_id, long_q, None,
        database_url=TEST_DATABASE_URL,
    )
    listing = client.get("/api/conversations", headers=operator_headers).json()["conversations"]
    title = next(c for c in listing if c["id"] == convo_id)["title"]
    assert len(title) <= store.TITLE_MAX
    assert title.endswith("…")


@requires_db
def test_conversations_are_listed_newest_first(client, operator_headers):
    first = _new_conversation(client, operator_headers, title="older")
    second = _new_conversation(client, operator_headers, title="newer")
    ids = [c["id"] for c in client.get("/api/conversations", headers=operator_headers).json()["conversations"]]
    assert ids.index(second) < ids.index(first)


# ---------------------------------------------------------------------------
# Ownership — a conversation id is not a secret, so it can't be the only check
# ---------------------------------------------------------------------------

@requires_db
def test_another_user_cannot_read_your_conversation(client, operator_headers, member_headers):
    convo_id = _new_conversation(client, operator_headers)
    resp = client.get(f"/api/conversations/{convo_id}", headers=member_headers)
    # 404, not 403: confirming the id exists would itself be a leak.
    assert resp.status_code == 404


@requires_db
def test_another_user_cannot_delete_your_conversation(client, operator_headers, member_headers):
    convo_id = _new_conversation(client, operator_headers)
    assert client.delete(f"/api/conversations/{convo_id}", headers=member_headers).status_code == 404
    # Still there for its actual owner.
    assert client.get(f"/api/conversations/{convo_id}", headers=operator_headers).status_code == 200


@requires_db
def test_another_user_cannot_rename_your_conversation(client, operator_headers, member_headers):
    convo_id = _new_conversation(client, operator_headers, title="mine")
    resp = client.patch(
        f"/api/conversations/{convo_id}", headers=member_headers, json={"title": "hijacked"}
    )
    assert resp.status_code == 404


@requires_db
def test_another_user_cannot_append_to_your_conversation(client, operator_headers, member_user):
    from app.history import store

    convo_id = _new_conversation(client, operator_headers)
    wrote = store.append_turn(
        convo_id, member_user.id, member_user.tenant_id, "sneaky", None,
        database_url=TEST_DATABASE_URL,
    )
    assert wrote is False


@requires_db
def test_your_conversation_list_only_contains_your_own(client, operator_headers, member_headers):
    mine = _new_conversation(client, operator_headers)
    theirs = _new_conversation(client, member_headers)

    my_ids = [c["id"] for c in client.get("/api/conversations", headers=operator_headers).json()["conversations"]]
    their_ids = [c["id"] for c in client.get("/api/conversations", headers=member_headers).json()["conversations"]]

    assert mine in my_ids and mine not in their_ids
    assert theirs in their_ids and theirs not in my_ids


@requires_db
def test_deleting_a_conversation_removes_its_messages(client, operator_headers, operator_user):
    from app.history import store

    convo_id = _new_conversation(client, operator_headers)
    store.append_turn(
        convo_id, operator_user.id, operator_user.tenant_id, "q", None,
        database_url=TEST_DATABASE_URL,
    )
    assert client.delete(f"/api/conversations/{convo_id}", headers=operator_headers).status_code == 200

    # ON DELETE CASCADE, asserted rather than assumed from the DDL.
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM app.messages WHERE conversation_id = %s", (convo_id,))
            assert cur.fetchone()[0] == 0


@requires_db
def test_a_missing_conversation_is_a_404(client, operator_headers):
    resp = client.get(
        "/api/conversations/00000000-0000-0000-0000-000000000000", headers=operator_headers
    )
    assert resp.status_code == 404
