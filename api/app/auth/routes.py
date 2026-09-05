"""Sign-up, sign-in, and account routes.

An account is identified by an email address OR a phone number — see
identifiers.py for why normalising the latter matters.

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
from pydantic import BaseModel, Field

from . import identifiers, reset, store
from .deps import Principal, current_principal, require_operator
from .security import issue_token

router = APIRouter(prefix="/api/auth", tags=["auth"])

MIN_PASSWORD_LENGTH = 8


def signup_enabled() -> bool:
    return os.environ.get("ALLOW_SIGNUP", "true").strip().lower() not in ("0", "false", "no", "off")


def default_signup_tenant() -> int:
    return int(os.environ.get("DEFAULT_SIGNUP_TENANT_ID", "1"))


class LoginRequest(BaseModel):
    identifier: str = Field(description="The email address or phone number on the account.")
    password: str


class SignupRequest(BaseModel):
    """`identifier` is an email address or a phone number — an account
    can be created with either.

    Note what is absent: no role, no tenant_id. Both are decided by the
    server. Adding either field here is a security regression, and
    test_auth.py asserts they stay absent.
    """

    identifier: str = Field(description="An email address or a phone number.")
    password: str = Field(min_length=MIN_PASSWORD_LENGTH)
    display_name: str | None = None


class CreateUserRequest(BaseModel):
    """What an operator may set when adding a teammate. Still no tenant —
    an operator can only add people to their own."""

    identifier: str = Field(description="An email address or a phone number.")
    password: str = Field(min_length=MIN_PASSWORD_LENGTH)
    display_name: str | None = None
    role: str = Field(default="member", pattern="^(member|operator)$")


def _split_identifier(raw: str) -> tuple[str | None, str | None]:
    """Turns one identifier into the (email, phone) pair the store wants.
    Raises a 400 on junk — at signup that's the user's typo and worth
    saying precisely, since it reveals nothing about existing accounts."""
    try:
        return identifiers.split(raw)
    except identifiers.IdentifierError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


class UserResponse(BaseModel):
    id: int
    email: str | None
    display_name: str | None
    tenant_id: int
    role: str
    phone: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse


def _to_response(user: store.User) -> UserResponse:
    return UserResponse(
        id=user.id, email=user.email, display_name=user.display_name,
        tenant_id=user.tenant_id, role=user.role, phone=user.phone,
    )


def _token_response(user: store.User) -> TokenResponse:
    token, expires_in = issue_token(
        user.id, user.tenant_id, user.role, user.email, user.password_changed_at
    )
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

    email, phone = _split_identifier(req.identifier)
    try:
        user = store.create_user(
            email=email,
            phone=phone,
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
            detail="that email or phone number can't be used — try signing in instead",
        ) from None
    return _token_response(user)


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest) -> TokenResponse:
    user = store.authenticate(req.identifier, req.password)
    if user is None:
        # One message for a wrong identifier, a wrong password, and a
        # malformed identifier alike — matching store.authenticate()'s
        # constant-time behaviour.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="incorrect email/phone or password",
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
    email, phone = _split_identifier(req.identifier)
    try:
        user = store.create_user(
            email=email, phone=phone, password=req.password,
            tenant_id=principal.tenant_id,
            role=req.role, display_name=req.display_name,
        )
    except store.DuplicateEmailError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _to_response(user)


# ---------------------------------------------------------------------------
# Password reset
#
# There is no mail or SMS provider wired up (see README), so a reset is
# generated here and handed over by an operator rather than emailed. The
# token machinery is delivery-agnostic: adding a sender later means
# delivering the same token and changing nothing below.
# ---------------------------------------------------------------------------

class ForgotPasswordRequest(BaseModel):
    identifier: str = Field(description="The email address or phone number on the account.")


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH)


@router.post("/password/forgot", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(req: ForgotPasswordRequest) -> dict:
    """Always answers the same way, whether or not the account exists.
    Anything else turns this into a "who has an account here" oracle, and
    that matters more than telling an honest user they typed it wrong."""
    generic = {
        "status": "accepted",
        "message": (
            "If that account exists, a reset has been prepared. "
            "This instance has no email or SMS delivery configured, so ask an "
            "operator for your reset link."
        ),
    }

    try:
        destination, _channel = identifiers.normalize(req.identifier)
    except identifiers.IdentifierError:
        return generic  # not even well-formed; say nothing different

    user = store.get_by_destination(destination)
    if user is None:
        return generic

    issued = reset.issue(user.id)
    # The operator's copy. Deliberately loud, and deliberately the only
    # place the token is ever legible.
    print(
        f"\n[password reset] for {identifiers.mask(destination, _channel)}\n"
        f"  token: {issued.token}\n"
        f"  expires: {issued.expires_at.isoformat()}\n"
        f"  hand this to them, or send the link: /reset?token={issued.token}\n"
    )
    return generic


@router.post("/password/reset", response_model=TokenResponse)
async def reset_password(req: ResetPasswordRequest) -> TokenResponse:
    outcome = reset.consume(req.token)
    if not outcome.ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=outcome.reason)

    if not store.set_password(outcome.user_id, req.new_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="that account no longer exists"
        )

    user = store.get_by_id(outcome.user_id)  # re-read for the new pin
    # Signed straight in: they've just proved control of the reset token
    # and set a password, so making them retype it immediately adds
    # nothing. Every OTHER session is now dead (auth/deps.py).
    return _token_response(user)


@router.post("/password/change", response_model=TokenResponse)
async def change_password(
    req: ChangePasswordRequest, principal: Principal = Depends(current_principal)
) -> TokenResponse:
    """Changing a password requires proving you know the current one, so
    a walked-away-from laptop can't be used to lock the owner out."""
    user = store.get_by_id(principal.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="account no longer exists")

    identifier = user.email or user.phone
    if identifier is None or store.authenticate(identifier, req.current_password) is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="that isn't your current password"
        )

    store.set_password(user.id, req.new_password)
    # Re-read: the fresh token has to pin the NEW password_changed_at, or
    # it would be refused by the very check it just triggered.
    return _token_response(store.get_by_id(user.id))
