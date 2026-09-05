"""Loads a .env file into the environment at import time.

Deliberately stdlib-only rather than pulling in python-dotenv: the format
this needs is a dozen lines of parsing, and the fewer dependencies sitting
in front of the credential path the better.

Real environment variables always win over the file, so a container's
injected secrets are never silently overridden by a .env someone left in
the image.

Search order (first file found wins): $QUERYWARDEN_ENV_FILE, ./.env,
api/.env, then the repo root's .env — so it works whether you run uvicorn
from api/ or from the repo root.
"""

from __future__ import annotations

import os
from pathlib import Path

_HERE = Path(__file__).resolve()
_API_DIR = _HERE.parents[1]      # api/
_REPO_ROOT = _HERE.parents[2]    # repo root


def candidate_paths() -> list[Path]:
    explicit = os.environ.get("QUERYWARDEN_ENV_FILE")
    paths = [Path(explicit)] if explicit else []
    paths += [Path.cwd() / ".env", _API_DIR / ".env", _REPO_ROOT / ".env"]
    return paths


def _parse_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].lstrip()
    if "=" not in line:
        return None

    key, _, value = line.partition("=")
    key = key.strip()
    value = value.strip()

    # Strip one matching pair of quotes. An App Password with spaces in it
    # ("abcd efgh ijkl mnop") only survives if it was quoted, so this has
    # to preserve what's inside rather than splitting on whitespace.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]

    return (key, value) if key else None


def load_env(path: Path | None = None, override: bool = False) -> dict[str, str]:
    """Reads the first .env found and sets anything not already in the
    environment. Returns what it applied. Missing file is not an error —
    running with no .env at all is a supported configuration."""
    paths = [path] if path is not None else candidate_paths()
    applied: dict[str, str] = {}

    for candidate in paths:
        try:
            if not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue

        for line in text.splitlines():
            parsed = _parse_line(line)
            if parsed is None:
                continue
            key, value = parsed
            if override or key not in os.environ:
                os.environ[key] = value
                applied[key] = value
        break  # first file found wins

    return applied
