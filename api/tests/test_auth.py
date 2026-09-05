"""Authentication and authorisation boundaries.

The headline test here is
`test_tenant_cannot_be_influenced_by_the_request_body`. Before auth
landed, `tenant_id` was a field on QueryRequest, which meant every
tenant control in db/00_schema.sql and db/02_roles.sql was faithfully
enforcing a number the caller picked. That test is the regression guard
for the whole class of bug — it fails the moment anyone reintroduces a
tenant field on a request model.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.auth.security import (
    TokenError,
    decode_token,
    hash_password,
    issue_token,
    verify_password,
)

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


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def test_password_round_trips():
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("Correct horse battery staple", stored)


def test_same_password_hashes_differently_each_time():
    # Per-hash salt: two users with the same password must not be
    # identifiable as such by comparing rows.
    assert hash_password("hunter2") != hash_password("hunter2")


def test_malformed_stored_hash_is_a_failed_login_not_a_crash():
    for junk in ("", "not-a-hash", "scrypt$oops", "bcrypt$1$2$3$4$5"):
        assert verify_password("anything", junk) is False


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def test_token_carries_the_tenant_as_a_signed_claim():
    token, expires_in = issue_token(7, 42, "member", "a@b.c")
    claims = decode_token(token)
    assert claims["tenant_id"] == 42
    assert claims["sub"] == "7"
    assert claims["role"] == "member"
    assert expires_in > 0


def test_a_tampered_token_is_rejected():
    token, _ = issue_token(1, 1, "member", "a@b.c")
    # Flip a character in the payload segment; the signature no longer matches.
    head, payload, sig = token.split(".")
    tampered = f"{head}.{payload[:-2]}XY.{sig}"
    with pytest.raises(TokenError):
        decode_token(tampered)


# ---------------------------------------------------------------------------
# Route protection
# ---------------------------------------------------------------------------

@requires_db
@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/api/query"),
        ("post", "/api/query/approve"),
        ("get", "/api/audit"),
        ("get", "/api/stats"),
        ("get", "/api/conversations"),
        ("post", "/api/conversations"),
        ("get", "/api/auth/me"),
    ],
)
def test_protected_routes_reject_anonymous_callers(client, method, path):
    # httpx won't send a body on GET; only the POSTs get one.
    resp = getattr(client, method)(path, json={}) if method == "post" else getattr(client, method)(path)
    assert resp.status_code == 401, f"{method.upper()} {path} was reachable without a token"


@requires_db
def test_garbage_token_is_rejected(client):
    resp = client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401


@requires_db
def test_login_succeeds_and_returns_the_users_tenant(client, operator_user):
    resp = client.post(
        "/api/auth/login", json={"identifier": operator_user.email, "password": TEST_PASSWORD}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["tenant_id"] == operator_user.tenant_id
    assert body["user"]["role"] == "operator"
    assert body["access_token"]


@requires_db
def test_wrong_password_is_rejected_without_revealing_the_account_exists(client, operator_user):
    wrong_password = client.post(
        "/api/auth/login", json={"identifier": operator_user.email, "password": "not-it"}
    )
    no_such_user = client.post(
        "/api/auth/login", json={"identifier": "nobody@querywarden.example.com", "password": "not-it"}
    )
    assert wrong_password.status_code == no_such_user.status_code == 401
    # Identical message: the response must not distinguish the two cases.
    assert wrong_password.json()["detail"] == no_such_user.json()["detail"]


@requires_db
def test_members_cannot_read_the_audit_log_or_stats(client, member_headers):
    assert client.get("/api/audit", headers=member_headers).status_code == 403
    assert client.get("/api/stats", headers=member_headers).status_code == 403


@requires_db
def test_operators_can_read_the_audit_log(client, operator_headers):
    assert client.get("/api/audit", headers=operator_headers).status_code == 200


# ---------------------------------------------------------------------------
# Sign-up
# ---------------------------------------------------------------------------

@requires_db
def test_signup_creates_an_account_and_returns_a_token(client, operator_user):
    email = _unique_email("signup-basic")
    resp = client.post(
        "/api/auth/signup", json={"identifier": email, "password": "a-good-password"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["access_token"]
    assert body["user"]["email"] == email
    # Signing in with the new account works straight away.
    assert client.post(
        "/api/auth/login", json={"identifier": email, "password": "a-good-password"}
    ).status_code == 200


@requires_db
def test_a_self_served_signup_is_a_member_not_an_operator(client, operator_user):
    """An account somebody made for themselves must not be able to read
    the audit log. Only the first account on a fresh instance, or an
    operator-created one, gets that."""
    resp = client.post(
        "/api/auth/signup",
        json={"identifier": _unique_email("signup-role"), "password": "a-good-password"},
    )
    assert resp.json()["user"]["role"] == "member"

    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    assert client.get("/api/audit", headers=headers).status_code == 403


@requires_db
def test_signup_cannot_choose_its_own_role_or_tenant(client, operator_user):
    """The regression guard for sign-up specifically: extra fields in the
    body are ignored, so /signup can never be a privilege-escalation or
    tenant-hopping route."""
    resp = client.post(
        "/api/auth/signup",
        json={
            "identifier": _unique_email("signup-escalate"),
            "password": "a-good-password",
            "role": "operator",   # ignored
            "tenant_id": 999,     # ignored
        },
    )
    assert resp.status_code == 201
    user = resp.json()["user"]
    assert user["role"] == "member"
    assert user["tenant_id"] != 999


@requires_db
def test_signup_request_model_has_no_role_or_tenant_field():
    from app.auth.routes import SignupRequest

    assert "role" not in SignupRequest.model_fields
    assert "tenant_id" not in SignupRequest.model_fields


@requires_db
def test_signup_rejects_a_short_password(client, operator_user):
    resp = client.post(
        "/api/auth/signup", json={"identifier": _unique_email("signup-short"), "password": "short"}
    )
    assert resp.status_code == 422


@requires_db
def test_signup_rejects_a_duplicate_email_without_confirming_it_exists(client, operator_user):
    resp = client.post(
        "/api/auth/signup",
        json={"identifier": operator_user.email, "password": "a-good-password"},
    )
    assert resp.status_code == 409
    # Must not name the address back — that's an existence oracle.
    assert operator_user.email not in resp.json()["detail"]


@requires_db
def test_signup_can_be_closed_by_configuration(client, operator_user, monkeypatch):
    monkeypatch.setenv("ALLOW_SIGNUP", "false")
    resp = client.post(
        "/api/auth/signup",
        json={"identifier": _unique_email("signup-closed"), "password": "a-good-password"},
    )
    assert resp.status_code == 403


@requires_db
def test_auth_config_reports_whether_signup_is_open(client, operator_user, monkeypatch):
    monkeypatch.setenv("ALLOW_SIGNUP", "true")
    body = client.get("/api/auth/config").json()
    assert body["setup_required"] is False
    assert body["signup_enabled"] is True
    assert body["min_password_length"] >= 8

    monkeypatch.setenv("ALLOW_SIGNUP", "false")
    assert client.get("/api/auth/config").json()["signup_enabled"] is False


@requires_db
def test_an_operator_may_still_create_an_operator(client, operator_headers):
    resp = client.post(
        "/api/auth/users",
        headers=operator_headers,
        json={"identifier": _unique_email("op-made"), "password": "a-good-password", "role": "operator"},
    )
    assert resp.status_code == 201
    assert resp.json()["role"] == "operator"


@requires_db
def test_a_member_cannot_create_accounts(client, member_headers):
    resp = client.post(
        "/api/auth/users",
        headers=member_headers,
        json={"identifier": "new@querywarden.example.com", "password": "abcdefgh"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# The regression guard for the tenant-injection bug
# ---------------------------------------------------------------------------

@requires_db
def test_tenant_cannot_be_influenced_by_the_request_body(client, monkeypatch):
    """A tenant_id in the body must be ignored, not honoured.

    Asserted where it actually matters: the value handed to the pipeline,
    which is what reaches set_config() and therefore the view predicates.
    """
    from app.pipeline import answer as answer_module

    seen: dict[str, int] = {}

    def fake_plan(ctx, question, tenant_id=1, model=None, on_event=None):
        seen["tenant_id"] = tenant_id
        raise RuntimeError("stop here — the tenant is all this test needs")

    monkeypatch.setattr(answer_module, "plan", fake_plan)

    member = _make_user_in_tenant("tenant-probe@querywarden.example.com", tenant_id=1)
    client.post(
        "/api/query",
        headers=auth_headers_for(member),
        json={"question": "how many orders", "tenant_id": 999},
    )

    assert seen["tenant_id"] == member.tenant_id
    assert seen["tenant_id"] != 999, "a request body field reached the tenant scope"


@requires_db
def test_query_request_model_has_no_tenant_field():
    """Belt and braces: the field is gone from the schema entirely, so it
    cannot be reintroduced silently by a merge."""
    from app.api.routes import ApproveRequest, QueryRequest

    assert "tenant_id" not in QueryRequest.model_fields
    assert "tenant_id" not in ApproveRequest.model_fields


def _make_user_in_tenant(email: str, tenant_id: int):
    from app.auth import store

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.users WHERE email = %s", (email,))
        conn.commit()
    return store.create_user(
        email=email, password=TEST_PASSWORD, tenant_id=tenant_id,
        role="member", database_url=TEST_DATABASE_URL,
    )


@requires_db
def test_chatbot_ro_has_zero_access_to_the_app_schema():
    """The same posture as the audit schema: a generated query must not be
    able to read a password hash or anyone's conversation history."""
    from app.pipeline.execute import chatbot_database_url

    with psycopg.connect(chatbot_database_url()) as conn:
        for table in ("app.users", "app.conversations", "app.messages"):
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(f"SELECT 1 FROM {table} LIMIT 1")
            conn.rollback()


def _unique_email(prefix: str) -> str:
    import uuid

    return f"{prefix}-{uuid.uuid4().hex[:10]}@querywarden.example.com"


# ---------------------------------------------------------------------------
# Signing up with a phone number instead of an email
# ---------------------------------------------------------------------------

def _unique_phone() -> str:
    import uuid

    return f"+1555{uuid.uuid4().int % 10**7:07d}"


@requires_db
def test_you_can_sign_up_with_a_phone_number(client, operator_user):
    phone = _unique_phone()
    resp = client.post(
        "/api/auth/signup", json={"identifier": phone, "password": "a-good-password"}
    )
    assert resp.status_code == 201, resp.text
    user = resp.json()["user"]
    assert user["phone"] == phone
    assert user["email"] is None
    assert user["role"] == "member"


@requires_db
def test_a_phone_account_can_sign_in_with_its_password(client, operator_user):
    phone = _unique_phone()
    client.post("/api/auth/signup", json={"identifier": phone, "password": "a-good-password"})
    resp = client.post(
        "/api/auth/login", json={"identifier": phone, "password": "a-good-password"}
    )
    assert resp.status_code == 200
    assert resp.json()["user"]["phone"] == phone


@requires_db
def test_a_phone_login_accepts_any_formatting_of_the_same_number(client, operator_user):
    """The stored number is E.164, so the form a person types has to
    normalise to it or they simply can't get back in."""
    # First digit 6-9: a real Indian mobile number never starts with 0
    # after the country code, and normalize_destination deliberately
    # strips a leading 0 as the national trunk prefix (098765… -> +9198765…).
    digits = f"{6 + uuid.uuid4().int % 4}{uuid.uuid4().int % 10**9:09d}"
    client.post(
        "/api/auth/signup", json={"identifier": f"+91{digits}", "password": "a-good-password"}
    )
    for typed in (f"+91 {digits[:5]} {digits[5:]}", f"+91-{digits}", digits):
        resp = client.post(
            "/api/auth/login", json={"identifier": typed, "password": "a-good-password"}
        )
        assert resp.status_code == 200, f"{typed!r} could not sign in"


@requires_db
def test_signing_up_with_the_same_number_twice_is_refused(client, operator_user):
    phone = _unique_phone()
    assert client.post(
        "/api/auth/signup", json={"identifier": phone, "password": "a-good-password"}
    ).status_code == 201
    resp = client.post(
        "/api/auth/signup", json={"identifier": phone, "password": "a-different-one"}
    )
    assert resp.status_code == 409


@requires_db
def test_signup_rejects_a_malformed_identifier(client, operator_user):
    resp = client.post(
        "/api/auth/signup", json={"identifier": "not-an-address", "password": "a-good-password"}
    )
    assert resp.status_code == 400


@requires_db
def test_a_malformed_login_identifier_is_a_401_not_a_400(client, operator_user):
    """Junk at login has to look exactly like a wrong password. A 400
    here would confirm which strings are even shaped like real accounts."""
    resp = client.post(
        "/api/auth/login", json={"identifier": "garbage", "password": "whatever"}
    )
    assert resp.status_code == 401


@requires_db
def test_an_operator_can_create_a_teammate_by_phone(client, operator_headers):
    phone = _unique_phone()
    resp = client.post(
        "/api/auth/users",
        headers=operator_headers,
        json={"identifier": phone, "password": "a-good-password"},
    )
    assert resp.status_code == 201
    assert resp.json()["phone"] == phone
