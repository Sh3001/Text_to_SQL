"""Shared fixtures. Guard tests need no database and must never be
skipped; schema/pipeline tests verify against a real querywarden
database, so they skip cleanly (not fail) when one isn't reachable.
"""

from __future__ import annotations

import os

import psycopg
import pytest

# The app's own trusted connection — never chatbot_ro. Introspection reads
# information_schema/pg_catalog, which is exactly what chatbot_ro is denied.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/querywarden"
)


def _db_reachable() -> bool:
    try:
        with psycopg.connect(TEST_DATABASE_URL, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(not _db_reachable(), reason="querywarden database not reachable")


@pytest.fixture()
def pg_conn():
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        yield conn


def _ollama_reachable() -> bool:
    try:
        import ollama

        ollama.Client().list()
        return True
    except Exception:
        return False


# Phase 03's generation layer runs against a local Ollama model (see
# api/app/llm/client.py for why: no Anthropic key was available). These
# tests exercise a real, running local model and are inherently slower and
# less than perfectly deterministic — skip cleanly, same as requires_db,
# rather than fail in an environment with no Ollama server.
requires_ollama = pytest.mark.skipif(not _ollama_reachable(), reason="Ollama server not reachable")


# ---------------------------------------------------------------------------
# Authentication fixtures
# ---------------------------------------------------------------------------
# Routes require a bearer token since auth landed (api/app/auth/). Tests
# get real tokens from real rows in app.users rather than a stubbed
# dependency override — the tenant-isolation properties these fixtures
# exist to prove are exactly what a stub would paper over.

# The app refuses to start without this; tests don't need a real secret,
# they need a consistent one. Set before app.main is imported anywhere.
os.environ.setdefault("JWT_SECRET", "test-only-secret-not-for-any-deployment")

TEST_OPERATOR_EMAIL = "pytest-operator@querywarden.example.com"
TEST_MEMBER_EMAIL = "pytest-member@querywarden.example.com"
TEST_PASSWORD = "pytest-password-123"


def _ensure_user(email: str, role: str, tenant_id: int = 1):
    """Get-or-create, so a re-run doesn't collide with the previous one."""
    from app.auth import store

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.users WHERE email = %s", (email,))
        conn.commit()
    return store.create_user(
        email=email, password=TEST_PASSWORD, tenant_id=tenant_id,
        role=role, database_url=TEST_DATABASE_URL,
    )


@pytest.fixture()
def operator_user():
    return _ensure_user(TEST_OPERATOR_EMAIL, "operator")


@pytest.fixture()
def member_user():
    return _ensure_user(TEST_MEMBER_EMAIL, "member")


def auth_headers_for(user) -> dict[str, str]:
    from app.auth.security import issue_token

    token, _ = issue_token(user.id, user.tenant_id, user.role, user.email)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def operator_headers(operator_user):
    return auth_headers_for(operator_user)


@pytest.fixture()
def member_headers(member_user):
    return auth_headers_for(member_user)


@pytest.fixture(scope="session", autouse=True)
def _clean_up_test_accounts():
    """Sign-up tests mint unique emails so repeated runs don't collide,
    which would otherwise leave them piling up in app.users. Everything
    on the reserved example.com test domain goes at the end of a session;
    conversations cascade with their owner."""
    yield
    if not _db_reachable():
        return
    try:
        with psycopg.connect(TEST_DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM app.users WHERE email LIKE %s", ("%@querywarden.example.com",))
                # Phone-only accounts from the signup tests use the
                # reserved +1555 range.
                cur.execute("DELETE FROM app.users WHERE phone LIKE %s", ("+1555%",))
            conn.commit()
    except Exception:  # noqa: BLE001 — cleanup must never fail a green run
        pass
