"""Email / phone identifier parsing.

Normalising is not cosmetic here. The normalised string is the lookup key
for every account, so if two spellings of one phone number produced two
strings, somebody would sign up typing it one way and be locked out
typing it another.
"""

from __future__ import annotations

import pytest

from app.auth.identifiers import Channel, IdentifierError, mask, normalize, split


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Someone@Example.COM", "someone@example.com"),
        ("  user@example.com  ", "user@example.com"),
    ],
)
def test_email_is_lowercased_and_trimmed(raw, expected):
    value, channel = normalize(raw)
    assert value == expected
    assert channel is Channel.EMAIL


@pytest.mark.parametrize(
    "raw",
    [
        "+919876543210",
        "+91 98765 43210",
        "+91-98765-43210",
        "+91 (98765) 43210",
        "0091 98765 43210",
        "09876543210",
        "9876543210",
    ],
)
def test_every_spelling_of_one_number_normalises_identically(raw):
    """The load-bearing test. All seven of these are the same number and
    must resolve to the same account."""
    assert normalize(raw)[0] == "+919876543210"


def test_phone_channel_is_detected():
    assert normalize("+919876543210")[1] is Channel.SMS


def test_a_leading_zero_is_treated_as_the_national_trunk_prefix():
    # 098765 43210 is how the number is dialled inside India; the 0 is
    # not part of it.
    assert normalize("09876543210")[0] == normalize("9876543210")[0]


@pytest.mark.parametrize(
    "raw", ["", "   ", "not-an-email@", "@nope.com", "12", "abcdefg", "+0123"]
)
def test_junk_is_rejected(raw):
    with pytest.raises(IdentifierError):
        normalize(raw)


def test_split_returns_exactly_one_of_email_or_phone():
    email, phone = split("someone@example.com")
    assert email == "someone@example.com" and phone is None

    email, phone = split("+91 98765 43210")
    assert email is None and phone == "+919876543210"


def test_masking_hides_the_identifying_part():
    masked_email = mask("someone@example.com", Channel.EMAIL)
    assert masked_email.endswith("@example.com")
    assert "someone" not in masked_email

    masked_phone = mask("+919876543210", Channel.SMS)
    assert masked_phone.startswith("+91") and masked_phone.endswith("10")
    assert "9876543" not in masked_phone
