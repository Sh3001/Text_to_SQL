"""Password reset tokens.

Shape of the thing: 256 bits of randomness, hashed at rest, single use,
one hour to live. Because the token is long there is no brute-force
surface, which is why — unlike a short passcode — this needs no attempt
ceiling.

Two details worth knowing:

  * The token is returned in the clear exactly once, at creation, and is
    never recoverable afterwards. Only its hash is stored, so an operator
    who loses it issues a new one rather than looking the old one up.

  * Verification can't look the row up BY the token, because only hashes
    are stored and scrypt is salted per row (the same token hashes
    differently every time). So the token carries its row id as a prefix:
    `<id>.<secret>`. The id selects the row, the secret is then verified
    against that row's hash. This is the standard shape — Django and
    Rails both do the same thing — and it leaks nothing, since the id is
    useless without the secret.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg

from .security import hash_password, verify_password

#: An hour is long enough to walk to someone's desk, short enough that a
#: token left in a chat log goes stale quickly.
TTL_SECONDS = 60 * 60

_SECRET_BYTES = 32


def app_database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/querywarden")


@dataclass(frozen=True)
class IssuedReset:
    token: str  # the only time this is ever available in the clear
    user_id: int
    expires_at: datetime


@dataclass(frozen=True)
class ResetOutcome:
    ok: bool
    user_id: int | None = None
    reason: str = ""


def issue(user_id: int, database_url: str | None = None) -> IssuedReset:
    """Creates a reset token, superseding any outstanding one for this
    user — two live tokens at once is more surface for no benefit."""
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=TTL_SECONDS)

    with psycopg.connect(database_url or app_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE app.password_resets SET consumed_at = now()
                WHERE user_id = %s AND consumed_at IS NULL AND expires_at > now()
                """,
                (user_id,),
            )
            cur.execute(
                """
                INSERT INTO app.password_resets (user_id, token_hash, expires_at)
                VALUES (%s, %s, %s) RETURNING id
                """,
                (user_id, hash_password(secret), expires_at),
            )
            row_id = cur.fetchone()[0]
        conn.commit()

    return IssuedReset(token=f"{row_id}.{secret}", user_id=user_id, expires_at=expires_at)


def _split(token: str) -> tuple[int, str] | None:
    row_id, _, secret = token.strip().partition(".")
    if not row_id or not secret:
        return None
    try:
        return int(row_id), secret
    except ValueError:
        return None


def consume(token: str, database_url: str | None = None) -> ResetOutcome:
    """Validates a token and marks it used, returning the user it belongs
    to. Every failure returns the same message: a token is either usable
    or it isn't, and distinguishing "expired" from "never existed" tells
    a guesser which ids are real."""
    generic = "That reset link is invalid or has expired. Ask for a new one."

    parsed = _split(token)
    if parsed is None:
        return ResetOutcome(False, reason=generic)
    row_id, secret = parsed

    with psycopg.connect(database_url or app_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id, token_hash, expires_at, consumed_at
                FROM app.password_resets WHERE id = %s
                """,
                (row_id,),
            )
            row = cur.fetchone()
            if row is None:
                return ResetOutcome(False, reason=generic)

            user_id, token_hash, expires_at, consumed_at = row
            if consumed_at is not None or expires_at <= datetime.now(timezone.utc):
                return ResetOutcome(False, reason=generic)
            if not verify_password(secret, token_hash):
                return ResetOutcome(False, reason=generic)

            # Consume before returning, so two concurrent submissions of
            # the same token can't both succeed.
            cur.execute(
                "UPDATE app.password_resets SET consumed_at = now() WHERE id = %s AND consumed_at IS NULL",
                (row_id,),
            )
            claimed = cur.rowcount == 1
        conn.commit()

    if not claimed:
        return ResetOutcome(False, reason=generic)
    return ResetOutcome(True, user_id=user_id)


def purge_expired(older_than_days: int = 7, database_url: str | None = None) -> int:
    """Housekeeping — the table keeps used rows on purpose and wants a sweep."""
    with psycopg.connect(database_url or app_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.password_resets WHERE created_at < now() - make_interval(days => %s)",
                (older_than_days,),
            )
            deleted = cur.rowcount
        conn.commit()
    return deleted
