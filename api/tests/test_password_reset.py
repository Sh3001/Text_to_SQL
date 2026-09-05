"""Password reset, password change, and the session invalidation both
trigger.

The test that matters most is
`test_a_reset_kills_a_session_issued_in_the_same_second`. Sessions are
stateless JWTs whose `iat` has whole-second resolution, so an earlier
implementation that compared `iat` against password_changed_at let a
session created in the same second as the reset survive it — exactly the
session a reset exists to kill. The fix pins the timestamp into the token
and compares it exactly; this test is what holds that in place.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.auth import reset, store

from .conftest import (  # noqa: F401
    TEST_DATABASE_URL,
    TEST_PASSWORD,
    auth_headers_for,
    operator_headers,
    operator_user,
    requires_db,
)


@pytest.fixture()
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def _fresh_email() -> str:
    return f"reset-{uuid.uuid4().hex[:10]}@querywarden.example.com"


def _new_account(client, password="original-password") -> tuple[str, str]:
    """Returns (identifier, access_token)."""
    email = _fresh_email()
    resp = client.post("/api/auth/signup", json={"identifier": email, "password": password})
    assert resp.status_code == 201, resp.text
    return email, resp.json()["access_token"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Token mechanics
# ---------------------------------------------------------------------------

@requires_db
def test_a_reset_token_is_never_stored_in_the_clear(client):
    email, _ = _new_account(client)
    user = store.get_by_destination(email, database_url=TEST_DATABASE_URL)
    issued = reset.issue(user.id, database_url=TEST_DATABASE_URL)

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT token_hash FROM app.password_resets WHERE user_id = %s ORDER BY id DESC LIMIT 1",
                (user.id,),
            )
            stored_hash = cur.fetchone()[0]

    _, _, secret = issued.token.partition(".")
    assert secret not in stored_hash
    assert stored_hash.startswith("scrypt$")


@requires_db
def test_a_token_works_once(client):
    email, _ = _new_account(client)
    user = store.get_by_destination(email, database_url=TEST_DATABASE_URL)
    issued = reset.issue(user.id, database_url=TEST_DATABASE_URL)

    first = reset.consume(issued.token, database_url=TEST_DATABASE_URL)
    assert first.ok and first.user_id == user.id

    second = reset.consume(issued.token, database_url=TEST_DATABASE_URL)
    assert not second.ok


@requires_db
def test_issuing_a_new_token_voids_the_previous_one(client):
    email, _ = _new_account(client)
    user = store.get_by_destination(email, database_url=TEST_DATABASE_URL)

    first = reset.issue(user.id, database_url=TEST_DATABASE_URL)
    second = reset.issue(user.id, database_url=TEST_DATABASE_URL)

    assert not reset.consume(first.token, database_url=TEST_DATABASE_URL).ok
    assert reset.consume(second.token, database_url=TEST_DATABASE_URL).ok


@requires_db
def test_an_expired_token_is_refused(client):
    email, _ = _new_account(client)
    user = store.get_by_destination(email, database_url=TEST_DATABASE_URL)
    issued = reset.issue(user.id, database_url=TEST_DATABASE_URL)

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.password_resets SET expires_at = %s WHERE user_id = %s",
                (datetime.now(timezone.utc) - timedelta(seconds=1), user.id),
            )
        conn.commit()

    assert not reset.consume(issued.token, database_url=TEST_DATABASE_URL).ok


@requires_db
@pytest.mark.parametrize(
    "token", ["", "garbage", "notanint.secret", "999999999.secret", "1.", ".secret"]
)
def test_malformed_or_unknown_tokens_are_refused(token):
    assert not reset.consume(token, database_url=TEST_DATABASE_URL).ok


@requires_db
def test_a_valid_id_with_the_wrong_secret_is_refused(client):
    """The id half of the token is not a secret; the other half is."""
    email, _ = _new_account(client)
    user = store.get_by_destination(email, database_url=TEST_DATABASE_URL)
    issued = reset.issue(user.id, database_url=TEST_DATABASE_URL)
    row_id, _, _ = issued.token.partition(".")

    assert not reset.consume(f"{row_id}.not-the-real-secret", database_url=TEST_DATABASE_URL).ok
    # ...and the real token still works, i.e. the failed attempt didn't burn it.
    assert reset.consume(issued.token, database_url=TEST_DATABASE_URL).ok


@requires_db
def test_every_failure_gives_the_same_message():
    """Distinguishing 'expired' from 'never existed' tells a guesser which
    ids are real."""
    reasons = {
        reset.consume(t, database_url=TEST_DATABASE_URL).reason
        for t in ("garbage", "999999999.secret", "1.wrong-secret")
    }
    assert len(reasons) == 1


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------

@requires_db
def test_forgot_password_says_the_same_thing_for_real_and_unknown_accounts(client):
    email, _ = _new_account(client)

    known = client.post("/api/auth/password/forgot", json={"identifier": email})
    unknown = client.post("/api/auth/password/forgot", json={"identifier": _fresh_email()})
    malformed = client.post("/api/auth/password/forgot", json={"identifier": "not-an-address"})

    assert known.status_code == unknown.status_code == malformed.status_code == 202
    assert known.json() == unknown.json() == malformed.json()


@requires_db
def test_the_full_reset_flow_changes_the_password(client):
    email, _ = _new_account(client, password="the-old-one")
    user = store.get_by_destination(email, database_url=TEST_DATABASE_URL)
    issued = reset.issue(user.id, database_url=TEST_DATABASE_URL)

    resp = client.post(
        "/api/auth/password/reset", json={"token": issued.token, "new_password": "the-new-one"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"]

    assert client.post(
        "/api/auth/login", json={"identifier": email, "password": "the-old-one"}
    ).status_code == 401
    assert client.post(
        "/api/auth/login", json={"identifier": email, "password": "the-new-one"}
    ).status_code == 200


@requires_db
def test_reset_rejects_a_short_password(client):
    email, _ = _new_account(client)
    user = store.get_by_destination(email, database_url=TEST_DATABASE_URL)
    issued = reset.issue(user.id, database_url=TEST_DATABASE_URL)
    resp = client.post(
        "/api/auth/password/reset", json={"token": issued.token, "new_password": "short"}
    )
    assert resp.status_code == 422


@requires_db
def test_a_spent_token_cannot_be_replayed_over_the_route(client):
    email, _ = _new_account(client)
    user = store.get_by_destination(email, database_url=TEST_DATABASE_URL)
    issued = reset.issue(user.id, database_url=TEST_DATABASE_URL)

    assert client.post(
        "/api/auth/password/reset", json={"token": issued.token, "new_password": "first-new"}
    ).status_code == 200
    assert client.post(
        "/api/auth/password/reset", json={"token": issued.token, "new_password": "second-new"}
    ).status_code == 400


# ---------------------------------------------------------------------------
# Session invalidation
# ---------------------------------------------------------------------------

@requires_db
def test_a_reset_kills_a_session_issued_in_the_same_second(client):
    """The regression guard. `iat` is whole seconds, so comparing it
    against password_changed_at let a session created in the same second
    as the reset survive — the exact session a reset exists to kill."""
    email, old_token = _new_account(client)
    assert client.get("/api/conversations", headers=_headers(old_token)).status_code == 200

    user = store.get_by_destination(email, database_url=TEST_DATABASE_URL)
    issued = reset.issue(user.id, database_url=TEST_DATABASE_URL)
    resp = client.post(
        "/api/auth/password/reset", json={"token": issued.token, "new_password": "a-new-password"}
    )
    new_token = resp.json()["access_token"]

    assert client.get("/api/conversations", headers=_headers(new_token)).status_code == 200
    assert client.get("/api/conversations", headers=_headers(old_token)).status_code == 401


@requires_db
def test_changing_a_password_kills_the_session_that_changed_it(client):
    email, token = _new_account(client, password="before-the-change")

    resp = client.post(
        "/api/auth/password/change",
        headers=_headers(token),
        json={"current_password": "before-the-change", "new_password": "after-the-change"},
    )
    assert resp.status_code == 200
    replacement = resp.json()["access_token"]

    assert client.get("/api/conversations", headers=_headers(replacement)).status_code == 200
    assert client.get("/api/conversations", headers=_headers(token)).status_code == 401


@requires_db
def test_change_password_requires_the_current_one(client):
    """Otherwise a walked-away-from laptop is enough to lock the owner out."""
    _, token = _new_account(client, password="the-real-one")
    resp = client.post(
        "/api/auth/password/change",
        headers=_headers(token),
        json={"current_password": "a-guess", "new_password": "attacker-chosen"},
    )
    assert resp.status_code == 403
    # The session is untouched by the failed attempt.
    assert client.get("/api/conversations", headers=_headers(token)).status_code == 200


@requires_db
def test_change_password_requires_authentication(client):
    resp = client.post(
        "/api/auth/password/change",
        json={"current_password": "x", "new_password": "yyyyyyyy"},
    )
    assert resp.status_code == 401


@requires_db
def test_a_token_with_no_password_pin_is_refused(client):
    """Fails closed: a session predating this check can't be verified, so
    it is rejected rather than trusted."""
    from app.auth.security import issue_token

    email, _ = _new_account(client)
    user = store.get_by_destination(email, database_url=TEST_DATABASE_URL)
    unpinned, _ = issue_token(user.id, user.tenant_id, user.role, user.email, None)

    assert client.get("/api/conversations", headers=_headers(unpinned)).status_code == 401


@requires_db
def test_chatbot_ro_cannot_read_reset_tokens():
    """Otherwise a generated query could lift a live token and take over
    an account."""
    from app.pipeline.execute import chatbot_database_url

    with psycopg.connect(chatbot_database_url()) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT token_hash FROM app.password_resets LIMIT 1")
        conn.rollback()
