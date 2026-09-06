"""Conversation history.

With authentication gone there are no accounts, so a thread belongs to a
tenant rather than a person and every thread is visible to anyone who can
reach the instance. The scoping that remains — and that these tests hold
— is by tenant.
"""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.tenant import current_tenant_id

from .conftest import TEST_DATABASE_URL, requires_db  # noqa: F401

OTHER_TENANT = 999


@pytest.fixture()
def other_tenant():
    """app.conversations.tenant_id is a real foreign key, so a second
    tenant has to exist before anything can be filed under it."""
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO analytics.tenants (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                (OTHER_TENANT, "pytest other tenant"),
            )
        conn.commit()
    yield OTHER_TENANT
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.conversations WHERE tenant_id = %s", (OTHER_TENANT,))
            cur.execute("DELETE FROM analytics.tenants WHERE id = %s", (OTHER_TENANT,))
        conn.commit()


@pytest.fixture()
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def _new_conversation(client, title=None) -> str:
    resp = client.post("/api/conversations", json={"title": title})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@requires_db
def test_a_new_conversation_starts_empty(client):
    convo_id = _new_conversation(client)
    resp = client.get(f"/api/conversations/{convo_id}")
    assert resp.status_code == 200
    assert resp.json()["messages"] == []


@requires_db
def test_a_turn_is_persisted_and_retitles_the_conversation(client):
    from app.history import store

    convo_id = _new_conversation(client)
    ok = store.append_turn(
        convo_id, current_tenant_id(),
        question="How many orders were shipped?",
        outcome={"verdict": "answered", "message": "answered", "execution": {"row_count": 1}},
        database_url=TEST_DATABASE_URL,
    )
    assert ok

    messages = client.get(f"/api/conversations/{convo_id}").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "How many orders were shipped?"
    # The whole outcome round-trips, so reloading replays what was seen
    # rather than re-running the query against changed data.
    assert messages[1]["outcome"]["verdict"] == "answered"

    listing = client.get("/api/conversations").json()["conversations"]
    titled = next(c for c in listing if c["id"] == convo_id)
    assert titled["title"] == "How many orders were shipped?"
    assert titled["message_count"] == 2


@requires_db
def test_a_long_question_is_elided_in_the_title(client):
    from app.history import store

    convo_id = _new_conversation(client)
    long_q = "What is the average " + "very " * 40 + "long question?"
    store.append_turn(convo_id, current_tenant_id(), long_q, None, database_url=TEST_DATABASE_URL)
    listing = client.get("/api/conversations").json()["conversations"]
    title = next(c for c in listing if c["id"] == convo_id)["title"]
    assert len(title) <= store.TITLE_MAX
    assert title.endswith("…")


@requires_db
def test_conversations_are_listed_newest_first(client):
    first = _new_conversation(client, title="older")
    second = _new_conversation(client, title="newer")
    ids = [c["id"] for c in client.get("/api/conversations").json()["conversations"]]
    assert ids.index(second) < ids.index(first)


# ---------------------------------------------------------------------------
# Tenant scoping — the only boundary left
# ---------------------------------------------------------------------------

@requires_db
def test_a_conversation_in_another_tenant_is_not_readable(client, other_tenant):
    """404 rather than 403: confirming the id exists would itself leak."""
    from app.history import store

    theirs = store.create_conversation(other_tenant, database_url=TEST_DATABASE_URL)
    assert client.get(f"/api/conversations/{theirs.id}").status_code == 404


@requires_db
def test_another_tenants_conversation_cannot_be_deleted_or_renamed(client, other_tenant):
    from app.history import store

    theirs = store.create_conversation(other_tenant, database_url=TEST_DATABASE_URL)
    assert client.delete(f"/api/conversations/{theirs.id}").status_code == 404
    assert client.patch(
        f"/api/conversations/{theirs.id}", json={"title": "hijacked"}
    ).status_code == 404
    # Still there for its own tenant.
    assert store.get_messages(theirs.id, other_tenant, database_url=TEST_DATABASE_URL) is not None


@requires_db
def test_another_tenants_conversation_cannot_be_appended_to(client, other_tenant):
    from app.history import store

    theirs = store.create_conversation(other_tenant, database_url=TEST_DATABASE_URL)
    wrote = store.append_turn(
        theirs.id, current_tenant_id(), "sneaky", None, database_url=TEST_DATABASE_URL
    )
    assert wrote is False


@requires_db
def test_the_listing_only_contains_this_tenants_threads(client, other_tenant):
    from app.history import store

    mine = _new_conversation(client, title="mine")
    theirs = store.create_conversation(other_tenant, database_url=TEST_DATABASE_URL)

    ids = [c["id"] for c in client.get("/api/conversations").json()["conversations"]]
    assert mine in ids
    assert theirs.id not in ids


@requires_db
def test_everyone_shares_one_history(client):
    """No accounts means no per-person threads: a conversation created by
    one caller is visible to the next. Asserted so the change is a stated
    property rather than a surprise."""
    created = _new_conversation(client, title="shared thread")
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as other_caller:
        ids = [c["id"] for c in other_caller.get("/api/conversations").json()["conversations"]]
    assert created in ids


@requires_db
def test_deleting_a_conversation_removes_its_messages(client):
    from app.history import store

    convo_id = _new_conversation(client)
    store.append_turn(convo_id, current_tenant_id(), "q", None, database_url=TEST_DATABASE_URL)
    assert client.delete(f"/api/conversations/{convo_id}").status_code == 200

    # ON DELETE CASCADE, asserted rather than assumed from the DDL.
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM app.messages WHERE conversation_id = %s", (convo_id,))
            assert cur.fetchone()[0] == 0


@requires_db
def test_a_missing_conversation_is_a_404(client):
    resp = client.get("/api/conversations/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
