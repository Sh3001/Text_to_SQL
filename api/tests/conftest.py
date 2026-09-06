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



@pytest.fixture(scope="session", autouse=True)
def _preserve_the_audit_log():
    """Save audit.query_log for the duration of the session and put it back.

    Several tests TRUNCATE this table so their row-count assertions
    ("total_queries == 1") aren't sensitive to execution order. That is
    reasonable in isolation, but the table is the running app's real
    activity trail — before this fixture, running the suite silently
    emptied the Activity tab of everything that had ever happened.

    Rows come back without their original `id` (the truncate restarts the
    sequence); nothing reads that column, and `request_id` — the identifier
    that actually means something — is preserved.
    """
    if not _db_reachable():
        yield
        return

    columns = (
        "request_id, occurred_at, tenant_id, question, model, verdict, failure_kind, "
        "generated_sql, safe_sql, edited, repair_attempts, row_count, duration_ms, message"
    )
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {columns} FROM audit.query_log ORDER BY id")
            saved = cur.fetchall()

    yield

    if not saved:
        return
    placeholders = ", ".join(["%s"] * len(saved[0]))
    try:
        with psycopg.connect(TEST_DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO audit.query_log ({columns}) VALUES ({placeholders})", saved
                )
            conn.commit()
    except Exception:  # noqa: BLE001 — restoring must never fail a green run
        pass


@pytest.fixture(scope="session", autouse=True)
def _clean_up_test_rows():
    """Delete every conversation the test session created.

    Scoped by creation time, not by title. An earlier version deleted a
    hardcoded list of titles, which silently missed every conversation
    that `append_turn` had renamed after its first question — so running
    the suite left a growing pile of "shared thread" and "How many orders
    were shipped?" rows in the sidebar of the real app. A fixed list can
    never cover what the code under test actually produces.

    Messages cascade with their conversation.
    """
    if not _db_reachable():
        yield
        return

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT now()")
            started_at = cur.fetchone()[0]

    yield

    try:
        with psycopg.connect(TEST_DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM app.conversations WHERE created_at >= %s", (started_at,)
                )
            conn.commit()
    except Exception:  # noqa: BLE001 — cleanup must never fail a green run
        pass
