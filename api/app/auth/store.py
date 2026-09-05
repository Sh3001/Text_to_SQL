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
    """That email already has an account."""


@dataclass(frozen=True)
class User:
    id: int
    email: str
    display_name: str | None
    tenant_id: int
    role: str

    @property
    def is_operator(self) -> bool:
        return self.role == "operator"


_SELECT = "SELECT id, email, display_name, tenant_id, role FROM app.users"


def _row_to_user(row) -> User:
    return User(id=row[0], email=row[1], display_name=row[2], tenant_id=row[3], role=row[4])


def create_user(
    email: str,
    password: str,
    tenant_id: int = 1,
    role: str = "member",
    display_name: str | None = None,
    database_url: str | None = None,
) -> User:
    with psycopg.connect(database_url or app_database_url()) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO app.users (email, password_hash, display_name, tenant_id, role)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id, email, display_name, tenant_id, role
                    """,
                    (email.strip(), hash_password(password), display_name, tenant_id, role),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise DuplicateEmailError(f"an account already exists for {email}") from exc
            user = _row_to_user(cur.fetchone())
        conn.commit()
    return user


def get_by_id(user_id: int, database_url: str | None = None) -> User | None:
    with psycopg.connect(database_url or app_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(f"{_SELECT} WHERE id = %s", (user_id,))
            row = cur.fetchone()
    return _row_to_user(row) if row else None


def authenticate(email: str, password: str, database_url: str | None = None) -> User | None:
    """Returns the user on a correct password, None otherwise.

    The hash is verified even when no account matches, against a dummy
    value, so that a wrong email and a wrong password take the same time.
    Skipping that turns the login route into an account-enumeration oracle.
    """
    with psycopg.connect(database_url or app_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, email, display_name, tenant_id, role, password_hash
                FROM app.users WHERE lower(email) = lower(%s)
                """,
                (email.strip(),),
            )
            row = cur.fetchone()

    if row is None:
        # Same work as a real verification, so timing doesn't leak existence.
        verify_password(password, hash_password("dummy"))
        return None

    if not verify_password(password, row[5]):
        return None
    return _row_to_user(row)


def count_users(database_url: str | None = None) -> int:
    with psycopg.connect(database_url or app_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM app.users")
            return cur.fetchone()[0]
