"""Runs guard-approved SQL against chatbot_ro — layer 4 of the defense
stack. Connects as chatbot_ro only, scopes the session to a tenant, and
always rolls back.

Tenant scoping uses set_config(), not `SET LOCAL app.tenant_id = %s` —
Postgres's SET doesn't take a bind parameter the way a function call
does, and set_config is exactly what db/02_roles.sql's views already
read via current_setting('app.tenant_id', true).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import psycopg

DEFAULT_CHATBOT_DATABASE_URL = "postgresql://chatbot_ro:chatbot_ro@localhost:5432/querywarden"


class ExecutionError(RuntimeError):
    """The database rejected guard-approved SQL — a real SQLSTATE from
    Postgres (a timeout, an unknown column the catalog was stale about).
    Surfaced as-is; pipeline/errors.py maps these onto the repair
    taxonomy."""

    def __init__(self, message: str, sqlstate: str | None = None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


@dataclass(frozen=True)
class ExecutionResult:
    columns: list[str]
    rows: list[tuple]
    row_count: int
    duration_ms: float


def chatbot_database_url() -> str:
    return os.environ.get("CHATBOT_DATABASE_URL", DEFAULT_CHATBOT_DATABASE_URL)


def execute(
    safe_sql: str,
    tenant_id: int,
    database_url: str | None = None,
    statement_timeout_ms: int | None = None,
) -> ExecutionResult:
    """`safe_sql` must already be guard.check()-approved — never call
    this with raw model output.

    `statement_timeout_ms` overrides chatbot_ro's role-level 15s default
    for this call only (via set_config, scoped to the transaction) —
    lets the timeout path (SQLSTATE 57014) be tested in milliseconds
    instead of waiting out a real 15-second timeout.
    """
    dsn = database_url or chatbot_database_url()
    with psycopg.connect(dsn) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
                if statement_timeout_ms is not None:
                    cur.execute("SELECT set_config('statement_timeout', %s, true)", (str(statement_timeout_ms),))
                t0 = time.monotonic()
                cur.execute(safe_sql)
                rows = cur.fetchall()
                duration_ms = (time.monotonic() - t0) * 1000
                columns = [d.name for d in cur.description] if cur.description else []
        except psycopg.Error as exc:
            sqlstate = getattr(exc.diag, "sqlstate", None)
            raise ExecutionError(str(exc).strip(), sqlstate=sqlstate) from exc
        finally:
            # chatbot_ro is already read-only by role default, but an
            # explicit rollback means a bug upstream can't persist a
            # write even under a future, less locked-down role.
            conn.rollback()

    return ExecutionResult(columns=columns, rows=rows, row_count=len(rows), duration_ms=duration_ms)
