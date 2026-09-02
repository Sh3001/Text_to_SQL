"""Loads semantic/catalog.yml — the hand-maintained business glossary,
canonical metrics, blessed join paths, deprecations, and value synonyms
described in the project plan's schema-intelligence section. Kept to a
single small loader rather than a class per section: the file is meant to
be read and edited by a domain expert, not modeled as application state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# semantic/catalog.yml lives at the repo root, three levels up from this
# file (api/app/schema/semantic.py -> api/app -> api -> repo root).
DEFAULT_PATH = Path(__file__).resolve().parents[3] / "semantic" / "catalog.yml"


def load(path: Path | str = DEFAULT_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    for required in ("glossary", "metrics", "join_paths", "deprecated", "value_synonyms"):
        if required not in data:
            raise ValueError(f"semantic/catalog.yml is missing required section '{required}'")
    return data


def value_synonyms(path: Path | str = DEFAULT_PATH) -> dict[str, dict[str, str]]:
    return load(path)["value_synonyms"]
