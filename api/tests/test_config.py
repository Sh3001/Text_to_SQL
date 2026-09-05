"""The .env loader.

Small file, but it sits directly in front of the credential path — a
mistake here is a mis-set credential or, worse, a real environment secret
silently overwritten by a stale file baked into an image.
"""

from __future__ import annotations

import os

import pytest

from app.config import load_env

# These tests are the one place that deliberately mutates os.environ, and
# load_env() writes whatever keys the fixture file names — which is more
# than any hand-maintained list would track. A previous version cleared a
# fixed set of keys, and a test .env containing ALLOW_SIGNUP=false leaked
# that setting into every later test in the session, closing sign-up and
# failing a dozen unrelated cases. Snapshot and restore the whole
# environment instead.
_KEYS_UNDER_TEST = (
    "JWT_SECRET", "DATABASE_URL", "CORS_ORIGINS", "DEFAULT_COUNTRY_CODE",
    "ALLOW_SIGNUP", "SOME_SETTING", "REAL_ONE", "EXPORTED",
)


@pytest.fixture(autouse=True)
def _isolated_environment():
    saved = dict(os.environ)
    # load_env only sets a key that isn't already present, so anything a
    # test asserts on has to start absent.
    for key in _KEYS_UNDER_TEST:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _write(tmp_path, body: str):
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


def test_values_are_loaded(tmp_path):
    load_env(_write(tmp_path, "DEFAULT_COUNTRY_CODE=+44\nALLOW_SIGNUP=false\n"))
    assert os.environ["DEFAULT_COUNTRY_CODE"] == "+44"


def test_a_real_environment_variable_wins(tmp_path, monkeypatch):
    """The important one. A container injects secrets as real env vars; a
    .env that happened to ship in the image must not override them."""
    monkeypatch.setenv("JWT_SECRET", "the-real-one")
    load_env(_write(tmp_path, "JWT_SECRET=the-stale-one-from-the-image\n"))
    assert os.environ["JWT_SECRET"] == "the-real-one"


def test_override_is_available_but_not_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("SOME_SETTING", "from-env")
    load_env(_write(tmp_path, "SOME_SETTING=from-file\n"), override=True)
    assert os.environ["SOME_SETTING"] == "from-file"


def test_quoted_values_keep_their_spaces(tmp_path):
    """Secrets and lists routinely contain spaces. Splitting on whitespace
    would silently truncate them, which fails as a wrong credential rather
    than as a parse error — the worst kind of bug to debug."""
    load_env(
        _write(
            tmp_path,
            'JWT_SECRET="a secret with spaces in it"\n'
            "CORS_ORIGINS='http://a.example, http://b.example'\n",
        )
    )
    assert os.environ["JWT_SECRET"] == "a secret with spaces in it"
    assert os.environ["CORS_ORIGINS"] == "http://a.example, http://b.example"


def test_unquoted_values_with_equals_signs_survive(tmp_path):
    """Connection strings and base64 secrets both contain '='."""
    load_env(_write(tmp_path, "DATABASE_URL=postgresql://u:p@h:5432/db?opt=1\n"))
    assert os.environ["DATABASE_URL"] == "postgresql://u:p@h:5432/db?opt=1"


def test_comments_blanks_and_export_prefixes_are_handled(tmp_path):
    applied = load_env(
        _write(
            tmp_path,
            "# a comment\n"
            "\n"
            "   \n"
            "REAL_ONE=yes\n"
            "export EXPORTED=also-yes\n"
            "not-a-pair\n",
        )
    )
    assert applied == {"REAL_ONE": "yes", "EXPORTED": "also-yes"}


def test_a_missing_file_is_not_an_error(tmp_path):
    # Running with no .env at all is a supported configuration.
    assert load_env(tmp_path / "definitely-not-here") == {}
