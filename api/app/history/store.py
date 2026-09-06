"""Conversation and message persistence over the trusted connection.

There are no accounts, so a thread belongs to a tenant rather than to a
person and every thread is visible to anyone who can reach the instance.
Reads are still scoped by tenant_id — that is what keeps one tenant's
history out of another's, and it is the only boundary left here.

Assistant turns store the whole serialized outcome as jsonb. Reloading a
thread replays exactly what the user saw rather than re-running the SQL,
which would quietly show different numbers as the warehouse changes.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import psycopg


def app_database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/querywarden")


#: Long questions get elided rather than stored as an unreadable title.
TITLE_MAX = 60


@dataclass(frozen=True)
class Conversation:
    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int = 0


@dataclass(frozen=True)
class Message:
    id: int
    role: str
    content: str
    outcome: dict | None
    created_at: str


def _title_from(question: str) -> str:
    q = " ".join(question.split())
    return q if len(q) <= TITLE_MAX else q[: TITLE_MAX - 1].rstrip() + "…"


def create_conversation(
    tenant_id: int, title: str | None = None, database_url: str | None = None
) -> Conversation:
    with psycopg.connect(database_url or app_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.conversations (tenant_id, title)
                VALUES (%s, COALESCE(%s, 'New conversation'))
                RETURNING id::text, title, created_at, updated_at
                """,
                (tenant_id, title),
            )
            row = cur.fetchone()
        conn.commit()
    return Conversation(id=row[0], title=row[1], created_at=str(row[2]), updated_at=str(row[3]))


def list_conversations(
    tenant_id: int, limit: int = 100, database_url: str | None = None
) -> list[Conversation]:
    with psycopg.connect(database_url or app_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id::text, c.title, c.created_at, c.updated_at,
                       count(m.id) AS message_count
                FROM app.conversations c
                LEFT JOIN app.messages m ON m.conversation_id = c.id
                WHERE c.tenant_id = %s
                GROUP BY c.id
                ORDER BY c.updated_at DESC
                LIMIT %s
                """,
                (tenant_id, limit),
            )
            return [
                Conversation(id=r[0], title=r[1], created_at=str(r[2]), updated_at=str(r[3]), message_count=r[4])
                for r in cur.fetchall()
            ]


def _in_tenant(cur, conversation_id: str, tenant_id: int) -> bool:
    cur.execute(
        "SELECT 1 FROM app.conversations WHERE id = %s AND tenant_id = %s",
        (conversation_id, tenant_id),
    )
    return cur.fetchone() is not None


def get_messages(
    conversation_id: str, tenant_id: int, database_url: str | None = None
) -> list[Message] | None:
    """None means the conversation doesn't exist *or* belongs to another
    tenant — deliberately the same answer, so the route returns 404 either
    way and doesn't confirm that another tenant's id is real."""
    with psycopg.connect(database_url or app_database_url()) as conn:
        with conn.cursor() as cur:
            if not _in_tenant(cur, conversation_id, tenant_id):
                return None
            cur.execute(
                """
                SELECT id, role, content, outcome, created_at
                FROM app.messages WHERE conversation_id = %s ORDER BY id
                """,
                (conversation_id,),
            )
            return [
                Message(id=r[0], role=r[1], content=r[2], outcome=r[3], created_at=str(r[4]))
                for r in cur.fetchall()
            ]


def append_turn(
    conversation_id: str,
    tenant_id: int,
    question: str,
    outcome: dict[str, Any] | None,
    database_url: str | None = None,
) -> bool:
    """Writes the user's question and the assistant's outcome as one unit,
    and retitles a still-untitled conversation from its first question.
    Returns False if the conversation isn't in this tenant."""
    with psycopg.connect(database_url or app_database_url()) as conn:
        with conn.cursor() as cur:
            if not _in_tenant(cur, conversation_id, tenant_id):
                return False

            cur.execute(
                "INSERT INTO app.messages (conversation_id, role, content) VALUES (%s, 'user', %s)",
                (conversation_id, question),
            )
            cur.execute(
                """
                INSERT INTO app.messages (conversation_id, role, content, outcome)
                VALUES (%s, 'assistant', %s, %s)
                """,
                (
                    conversation_id,
                    (outcome or {}).get("message", ""),
                    json.dumps(outcome, default=str) if outcome is not None else None,
                ),
            )
            cur.execute(
                """
                UPDATE app.conversations
                SET updated_at = now(),
                    title = CASE WHEN title = 'New conversation' THEN %s ELSE title END
                WHERE id = %s
                """,
                (_title_from(question), conversation_id),
            )
        conn.commit()
    return True


def delete_conversation(
    conversation_id: str, tenant_id: int, database_url: str | None = None
) -> bool:
    with psycopg.connect(database_url or app_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.conversations WHERE id = %s AND tenant_id = %s",
                (conversation_id, tenant_id),
            )
            deleted = cur.rowcount > 0
        conn.commit()
    return deleted


def rename_conversation(
    conversation_id: str, tenant_id: int, title: str, database_url: str | None = None
) -> bool:
    with psycopg.connect(database_url or app_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE app.conversations SET title = %s, updated_at = now()
                WHERE id = %s AND tenant_id = %s
                """,
                (_title_from(title), conversation_id, tenant_id),
            )
            renamed = cur.rowcount > 0
        conn.commit()
    return renamed
