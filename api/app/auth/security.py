"""Password hashing and token signing.

Passwords use stdlib scrypt rather than bcrypt/argon2: it's memory-hard,
it's in the standard library (no compiled dependency to build in a slim
container), and the parameters below are the interactive-login settings
from RFC 7914. The stored format carries its own salt and parameters, so
raising the cost later doesn't invalidate existing hashes — verify()
reads the parameters out of the stored string rather than assuming the
current constants.

Tokens are HS256 JWTs. The secret comes from JWT_SECRET with no default:
a signing key that falls back to a constant is the same bug as a database
password that falls back to a constant.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt

# RFC 7914's interactive-login parameters. n is the work factor; raising
# it makes every future hash slower and is safe to do at any time.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32
_SALT_BYTES = 16

TOKEN_TTL_HOURS = 12
_ALGORITHM = "HS256"


class AuthConfigError(RuntimeError):
    """The environment is missing something authentication cannot run
    without. Raised at import/startup, never mid-request."""


def jwt_secret() -> str:
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        raise AuthConfigError(
            "JWT_SECRET is not set. Generate one with "
            "`python -c 'import secrets; print(secrets.token_urlsafe(48))'` "
            "and set it in the environment — there is deliberately no default."
        )
    return secret


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Returns `scrypt$<n>$<r>$<p>$<salt hex>$<key hex>`."""
    salt = secrets.token_bytes(_SALT_BYTES)
    key = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time comparison against a stored hash. Never raises on a
    malformed stored value — a corrupt row is a failed login, not a 500."""
    try:
        scheme, n, r, p, salt_hex, key_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p),
            dklen=len(bytes.fromhex(key_hex)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, bytes.fromhex(key_hex))


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def issue_token(
    user_id: int, tenant_id: int, role: str, email: str | None, password_changed_at=None
) -> tuple[str, int]:
    """Returns (token, expires_in_seconds).

    tenant_id is baked into the signed payload deliberately. That is the
    whole point of this module: the tenant a request runs under is now a
    signed claim rather than a field the caller fills in.

    `pwd_at` pins the account's password_changed_at at issue time, and
    auth/deps.py refuses any token whose pin no longer matches. An exact
    match is used rather than comparing `iat` against the timestamp,
    because `iat` has whole-second resolution: a password reset and a
    login in the same second are indistinguishable by time, so the old
    session would survive exactly the reset that was meant to kill it.
    """
    expires_at = datetime.now(timezone.utc) + timedelta(hours=TOKEN_TTL_HOURS)
    payload = {
        "sub": str(user_id),
        "tenant_id": tenant_id,
        "role": role,
        "email": email,
        "exp": expires_at,
        "iat": datetime.now(timezone.utc),
        "pwd_at": password_changed_at.isoformat() if password_changed_at is not None else None,
    }
    return jwt.encode(payload, jwt_secret(), algorithm=_ALGORITHM), TOKEN_TTL_HOURS * 3600


class TokenError(Exception):
    """The token is missing, malformed, expired, or signed with the wrong
    key. All four are the same thing to a caller: not authenticated."""


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, jwt_secret(), algorithms=[_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError(f"invalid token: {exc}") from exc
