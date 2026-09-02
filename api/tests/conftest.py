"""Shared fixtures for the Phase 02 schema-intelligence tests.

Guard tests (Phase 01) need no database and must never be skipped — that
was the whole point of building the guard before the generator. Schema
tests are different: they verify introspection against a real, live
querywarden database, so they skip cleanly (not fail) when one isn't
reachable, matching how CI vs. local-with-Docker environments will differ.
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
