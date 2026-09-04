"""Adapts a Snapshot to the guard's `Catalog` protocol so UNKNOWN_TABLE
rejections come with real "did you mean" suggestions. No DB round-trip —
holds the relation-name set in memory, fuzzy-matches with difflib."""

from __future__ import annotations

import difflib

from .introspect import Snapshot


class SnapshotCatalog:
    def __init__(self, snapshot: Snapshot) -> None:
        self._schema = snapshot.schema
        self._names = snapshot.relation_names

    def has_relation(self, schema: str | None, name: str) -> bool:
        # Outside our own schema isn't this catalog's concern — the
        # guard's denylists cover system schemas already.
        if schema is not None and schema != self._schema:
            return True
        return name in self._names

    def nearest_relations(self, name: str, limit: int = 3) -> list[str]:
        return difflib.get_close_matches(name, sorted(self._names), n=limit, cutoff=0.4)
