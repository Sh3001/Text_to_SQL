"""FastAPI dependencies that turn an Authorization header into a
Principal — the authenticated caller, carrying the tenant every
downstream query runs under.

The important property: nothing downstream of here ever reads a tenant
from request input. `Principal.tenant_id` comes out of a signed token,
which came out of app.users at login. That is the only path.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status

from .security import TokenError, decode_token


@dataclass(frozen=True)
class Principal:
    user_id: int
    email: str
    tenant_id: int
    role: str

    @property
    def is_operator(self) -> bool:
        return self.role == "operator"


_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


async def current_principal(authorization: str | None = Header(default=None)) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _UNAUTHENTICATED

    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = decode_token(token)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    try:
        principal = Principal(
            user_id=int(claims["sub"]),
            email=claims["email"],
            tenant_id=int(claims["tenant_id"]),
            role=claims["role"],
        )
        password_pin = claims["pwd_at"]
    except (KeyError, ValueError, TypeError) as exc:
        # A correctly-signed token with the wrong shape means the signing
        # key is shared with something that isn't this app. Refuse it.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="malformed token payload"
        ) from exc

    _reject_if_password_changed(principal.user_id, password_pin)
    return principal


def _reject_if_password_changed(user_id: int, password_pin: str | None) -> None:
    """Cuts every other session loose when a password changes.

    Tokens here are stateless, so without this check a stolen session
    would survive a reset for the token's full 12-hour life — precisely
    the moment the victim most expects to be safe. That is worth one
    small query per authenticated request, and it is the strongest
    argument for the connection pool named in the deployment notes, since
    every request currently opens its own connection.

    The comparison is an exact match on the pinned timestamp, not `iat`
    against the clock: `iat` is whole seconds, so a reset and a login in
    the same second are indistinguishable by time and the old session
    would survive. A token with no pin at all predates this check and is
    refused rather than trusted — failing closed costs one sign-in.
    """
    from . import store

    changed_at = store.password_changed_at(user_id)
    if changed_at is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="account no longer exists"
        )
    if password_pin is None or password_pin != changed_at.isoformat():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="your password changed — please sign in again",
        )


async def require_operator(principal: Principal = Depends(current_principal)) -> Principal:
    """Gates the audit log and the stats dashboard. Both expose other
    people's questions and the SQL of blocked attempts, which is a
    probing oracle in the wrong hands."""
    if not principal.is_operator:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this view requires an operator account",
        )
    return principal
