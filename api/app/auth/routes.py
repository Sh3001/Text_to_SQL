"""Sign-up, sign-in, and account routes.

Sign-up is open by default (set ALLOW_SIGNUP=false to close it), but it
is deliberately not a way to choose who you are. A self-served account
always lands as a `member` in DEFAULT_SIGNUP_TENANT_ID. Role and tenant
are decided here, never read from the request — accepting either from
the body would reintroduce exactly the bug that adding auth fixed.

The one exception is the very first account on a fresh instance: it
becomes an operator, because somebody has to be able to read the audit
log and there is nobody yet to grant it.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from . import store
from .deps import Principal, current_principal, require_operator
from .security import issue_token

router = APIRouter(prefix="/api/auth", tags=["auth"])

MIN_PASSWORD_LENGTH = 8


def signup_enabled() -> bool:
    return os.environ.get("ALLOW_SIGNUP", "true").strip().lower() not in ("0", "false", "no", "off")


def default_signup_tenant() -> int:
    return int(os.environ.get("DEFAULT_SIGNUP_TENANT_ID", "1"))


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class SignupRequest(BaseModel):
    """Note what is absent: no role, no tenant_id. Both are decided by
    the server. Adding either field here is a security regression, and
    test_auth.py asserts they stay absent."""

    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH)
    display_name: str | None = None


class CreateUserRequest(BaseModel):
    """What an operator may set when adding a teammate. Still no tenant —
    an operator can only add people to their own."""

    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH)
    display_name: str | None = None
    role: str = Field(default="member", pattern="^(member|operator)$")


class UserResponse(BaseModel):
    id: int
    email: str
    display_name: str | None
    tenant_id: int
    role: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse


def _to_response(user: store.User) -> UserResponse:
    return UserResponse(
        id=user.id, email=user.email, display_name=user.display_name,
        tenant_id=user.tenant_id, role=user.role,
    )


def _token_response(user: store.User) -> TokenResponse:
    token, expires_in = issue_token(user.id, user.tenant_id, user.role, user.email)
    return TokenResponse(access_token=token, expires_in=expires_in, user=_to_response(user))


@router.get("/config")
async def auth_config() -> dict:
    """Lets the sign-in screen render the right thing: a bootstrap prompt
    on a fresh instance, a sign-up tab when open, neither when closed."""
    first_run = store.count_users() == 0
    return {
        "setup_required": first_run,
        "signup_enabled": first_run or signup_enabled(),
        "min_password_length": MIN_PASSWORD_LENGTH,
    }


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def signup(req: SignupRequest) -> TokenResponse:
    is_first_user = store.count_users() == 0

    if not is_first_user and not signup_enabled():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="sign-up is closed on this instance — ask an operator for an account",
        )

    try:
        user = store.create_user(
            email=req.email,
            password=req.password,
            # Server-decided, both of them.
            tenant_id=default_signup_tenant(),
            role="operator" if is_first_user else "member",
            display_name=req.display_name,
        )
    except store.DuplicateEmailError:
        # Deliberately vague: a precise "that email is taken" turns
        # sign-up into an account-existence oracle.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="that email can't be used — try signing in instead",
        ) from None
    return _token_response(user)


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest) -> TokenResponse:
    user = store.authenticate(req.email, req.password)
    if user is None:
        # One message for both wrong-email and wrong-password, matching
        # store.authenticate()'s constant-time behaviour.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="incorrect email or password"
        )
    return _token_response(user)


@router.get("/me", response_model=UserResponse)
async def me(principal: Principal = Depends(current_principal)) -> UserResponse:
    user = store.get_by_id(principal.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="account no longer exists")
    return _to_response(user)


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    req: CreateUserRequest, principal: Principal = Depends(require_operator)
) -> UserResponse:
    """Operators add teammates. New accounts land in the operator's own
    tenant — an operator cannot mint a user into somebody else's data."""
    try:
        user = store.create_user(
            email=req.email, password=req.password, tenant_id=principal.tenant_id,
            role=req.role, display_name=req.display_name,
        )
    except store.DuplicateEmailError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _to_response(user)
