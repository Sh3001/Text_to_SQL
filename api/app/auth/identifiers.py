"""Parsing and normalising the thing a person types to identify
themselves: an email address or a phone number.

This exists because an account can be created with either, so every path
that looks a user up — sign-up, sign-in, the CLI — has to agree on what
string a given input means. Normalising phone numbers to E.164 is the
load-bearing part: without it somebody could sign up typing
"+91 98765 43210" and be locked out typing "09876543210", which is the
same number.
"""

from __future__ import annotations

import os
import re
from enum import Enum


class Channel(str, Enum):
    EMAIL = "email"
    SMS = "sms"


class IdentifierError(ValueError):
    """The input isn't a usable email address or phone number. The message
    is written to be shown to the person who typed it."""


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_CLEAN_RE = re.compile(r"[\s\-().]")
_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")

#: Applied to a number typed without one.
DEFAULT_COUNTRY_CODE = os.environ.get("DEFAULT_COUNTRY_CODE", "+91")


def normalize(raw: str) -> tuple[str, Channel]:
    """Returns (normalised identifier, channel), or raises IdentifierError."""
    value = raw.strip()
    if not value:
        raise IdentifierError("Enter an email address or phone number.")

    if "@" in value:
        email = value.lower()
        if not _EMAIL_RE.match(email):
            raise IdentifierError("That doesn't look like a valid email address.")
        return email, Channel.EMAIL

    cleaned = _PHONE_CLEAN_RE.sub("", value)
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    if not cleaned.startswith("+"):
        # A bare national number is the common case in a form. The leading
        # zero, where present, is the national trunk prefix and is not part
        # of the number: 098765 43210 -> +91 98765 43210.
        cleaned = DEFAULT_COUNTRY_CODE + cleaned.lstrip("0")
    if not _E164_RE.match(cleaned):
        raise IdentifierError(
            "That doesn't look like a valid phone number. Include the country code."
        )
    return cleaned, Channel.SMS


def split(raw: str) -> tuple[str | None, str | None]:
    """Normalises, then returns the (email, phone) pair the user store
    wants — exactly one of which is set."""
    value, channel = normalize(raw)
    return (value, None) if channel is Channel.EMAIL else (None, value)


def mask(identifier: str, channel: Channel) -> str:
    """A form a person recognises without it being useful to somebody
    reading over their shoulder."""
    if channel is Channel.EMAIL:
        name, _, domain = identifier.partition("@")
        shown = name[0] if name else ""
        return f"{shown}{'•' * max(len(name) - 1, 1)}@{domain}"
    return f"{identifier[:3]}{'•' * max(len(identifier) - 5, 1)}{identifier[-2:]}"
