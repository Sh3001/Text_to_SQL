"""Adapts a Snapshot to the `Catalog` protocol the AST guard consumes
(guards/ast_guard.py) so UNKNOWN_TABLE rejections come with real "did you
mean" suggestions instead of a bare no.

Deliberately no DB round-trip here: the guard runs on every generated
query and needs an answer in microseconds, so this holds the relation-name
set in memory and does fuzzy suggestion matching with stdlib difflib
rather than a live pg_trgm lookup.
"""

from __future__ import annotations

import difflib

from .introspect import Snapshot


class SnapshotCatalog:
    def __init__(self, snapshot: Snapshot) -> None:
        self._schema = snapshot.schema
        self._names = snapshot.relation_names

    def has_relation(self, schema: str | None, name: str) -> bool:
        # Anything outside our own schema isn't this catalog's concern —
        # the guard's DENIED_SCHEMAS/DENIED_UNQUALIFIED_RELATIONS lists
        # already cover pg_catalog/information_schema, and any other
        # schema name would fail for lack of a grant at execution time
        # regardless of what this method says.
        if schema is not None and schema != self._schema:
            return True
        return name in self._names

    def nearest_relations(self, name: str, limit: int = 3) -> list[str]:
        return difflib.get_close_matches(name, sorted(self._names), n=limit, cutoff=0.4)
