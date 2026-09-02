"""Runs guard-approved SQL against chatbot_ro — layer 4 of the defense
stack (see the project plan). A minimal execution envelope: connects as
chatbot_ro (never anything more privileged), scopes the session to a
tenant, and always rolls back. The harder parts of "execution envelope" —
cursor-paged fetch instead of a single fetchall, retry/backoff, connection
pooling — are Phase 04 hardening; this is enough to close the
generate -> guard -> execute loop end to end.

Tenant scoping uses set_config(), not `SET LOCAL app.tenant_id = %s` —
Postgres's SET command doesn't take a normal bind parameter the way a
function call does, so set_config is both the safe-parameterization
answer and, not coincidentally, exactly what db/02_roles.sql's views
already read via current_setting('app.tenant_id', true).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import psycopg

DEFAULT_CHATBOT_DATABASE_URL = "postgresql://chatbot_ro:chatbot_ro@localhost:5432/querywarden"


class ExecutionError(RuntimeError):
    """The database rejected guard-approved SQL — a real SQLSTATE from
    Postgres (syntax the parser accepted differently than the executor,
    a timeout, an unknown column the catalog snapshot was stale about).
    Phase 04 maps these onto the plan's error taxonomy for repair; here
    they're surfaced as-is.
    """

    def __init__(self, message: str, sqlstate: str | None = None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


@dataclass(frozen=True)
class ExecutionResult:
    columns: list[str]
    rows: list[tuple]
    row_count: int
    duration_ms: float


def _database_url() -> str:
    return os.environ.get("CHATBOT_DATABASE_URL", DEFAULT_CHATBOT_DATABASE_URL)


def execute(safe_sql: str, tenant_id: int, database_url: str | None = None) -> ExecutionResult:
    """`safe_sql` must already be guard.check()-approved — this function
    does not re-validate it. Never call this with raw model output.
    """
    dsn = database_url or _database_url()
    with psycopg.connect(dsn) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
                t0 = time.monotonic()
                cur.execute(safe_sql)
                rows = cur.fetchall()
                duration_ms = (time.monotonic() - t0) * 1000
                columns = [d.name for d in cur.description] if cur.description else []
        except psycopg.Error as exc:
            sqlstate = getattr(exc.diag, "sqlstate", None)
            raise ExecutionError(str(exc).strip(), sqlstate=sqlstate) from exc
        finally:
            # chatbot_ro's session is already read-only by role default
            # (db/02_roles.sql), but rolling back explicitly means a bug
            # anywhere upstream can't accidentally persist a write even in
            # a future role that's less locked down.
            conn.rollback()

    return ExecutionResult(columns=columns, rows=rows, row_count=len(rows), duration_ms=duration_ms)
