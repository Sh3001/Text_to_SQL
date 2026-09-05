"""Reads and writes app.users over the application's own trusted
connection. Never chatbot_ro — that role cannot see the app schema at
all, which is the point (db/04_app.sql)."""

from __future__ import annotations

import os
from dataclasses import dataclass

import psycopg

from .security import hash_password, verify_password


def app_database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/querywarden")


class DuplicateEmailError(RuntimeError):
    """That email or phone number already has an account."""


@dataclass(frozen=True)
class User:
    id: int
    email: str | None
    display_name: str | None
    tenant_id: int
    role: str
    phone: str | None = None

    @property
    def is_operator(self) -> bool:
        return self.role == "operator"

    @property
    def label(self) -> str:
        """What to show when there's no display name. An account created
        from a phone number has no email at all."""
        return self.display_name or self.email or self.phone or f"user {self.id}"


_SELECT = "SELECT id, email, display_name, tenant_id, role, phone FROM app.users"


def _row_to_user(row) -> User:
    return User(
        id=row[0], email=row[1], display_name=row[2],
        tenant_id=row[3], role=row[4], phone=row[5],
    )


def create_user(
    email: str | None = None,
    password: str | None = None,
    tenant_id: int = 1,
    role: str = "member",
    display_name: str | None = None,
    phone: str | None = None,
    database_url: str | None = None,
) -> User:
    """At least one of email/phone is required (enforced by a CHECK in
    db/05_phone_identity.sql)."""
    if not email and not phone:
        raise ValueError("a user needs an email or a phone number")

    with psycopg.connect(database_url or app_database_url()) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO app.users (email, password_hash, display_name, tenant_id, role, phone)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id, email, display_name, tenant_id, role, phone
                    """,
                    (
                        email.strip() if email else None,
                        hash_password(password) if password else None,
                        display_name, tenant_id, role, phone,
                    ),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise DuplicateEmailError(
                    f"an account already exists for {email or phone}"
                ) from exc
            user = _row_to_user(cur.fetchone())
        conn.commit()
    return user


def get_by_destination(destination: str, database_url: str | None = None) -> User | None:
    """Look up by email or phone — whichever the destination is. Email is
    matched case-insensitively; phone is already normalised to E.164."""
    with psycopg.connect(database_url or app_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"{_SELECT} WHERE lower(email) = lower(%s) OR phone = %s",
                (destination, destination),
            )
            row = cur.fetchone()
    return _row_to_user(row) if row else None


def get_by_id(user_id: int, database_url: str | None = None) -> User | None:
    with psycopg.connect(database_url or app_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(f"{_SELECT} WHERE id = %s", (user_id,))
            row = cur.fetchone()
    return _row_to_user(row) if row else None


def authenticate(identifier: str, password: str, database_url: str | None = None) -> User | None:
    """Returns the user on a correct password, None otherwise.

    `identifier` is an email address or a phone number — whichever the
    account was created with, normalised by identifiers.py so
    "+91 98765 43210" and "+919876543210" reach the same row.

    The hash is verified even when no account matches, against a dummy
    value, so that a wrong identifier and a wrong password take the same
    time. Skipping that turns login into an account-enumeration oracle.
    """
    from .identifiers import IdentifierError, normalize

    try:
        destination, _channel = normalize(identifier)
    except IdentifierError:
        # Junk input is simply a failed login, not a different error —
        # saying "that isn't a valid email" here would leak which
        # identifiers are even shaped like real accounts.
        verify_password(password, hash_password("dummy"))
        return None

    with psycopg.connect(database_url or app_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, email, display_name, tenant_id, role, phone, password_hash
                FROM app.users WHERE lower(email) = lower(%s) OR phone = %s
                """,
                (destination, destination),
            )
            row = cur.fetchone()

    if row is None:
        # Same work as a real verification, so timing doesn't leak existence.
        verify_password(password, hash_password("dummy"))
        return None

    stored_hash = row[6]
    if stored_hash is None:
        # An account row with no password set. Not worth distinguishing —
        # from the caller's side it's simply a failed login.
        verify_password(password, hash_password("dummy"))
        return None
    if not verify_password(password, stored_hash):
        return None
    return _row_to_user(row)


def count_users(database_url: str | None = None) -> int:
    with psycopg.connect(database_url or app_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM app.users")
            return cur.fetchone()[0]
