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
