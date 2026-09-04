"""The value index — resolves a literal in a question to the real value in
a low-cardinality column (e.g. `region = 'California'` when the column
stores `'CA'`) before it reaches generated SQL.

Two different problems: TYPOS/CASING ("califronia" -> "california") are a
nearby-string problem edit-distance solves; ABBREVIATIONS ("California"
-> "CA") are not — the strings share no overlap, so similarity can't find
them. That needs an explicit lookup from semantic/catalog.yml's
value_synonyms. resolve() tries synonyms first, then falls back to fuzzy
matching in-memory (difflib) against values discovered via the trusted
connection, never chatbot_ro.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psycopg

#: A column qualifies if it has at most this many distinct values.
MAX_DISTINCT_VALUES = 500

#: Values shorter than this are never fuzzy-matched — edit-distance
#: similarity is unreliable at this length (see resolve()).
MIN_FUZZY_TARGET_LENGTH = 4

#: Explicit (view, column) list rather than auto-discovering every text
#: column — a high-cardinality column (name, review body) would pass a
#: naive distinct-count check on a small dataset.
CANDIDATE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("v_customers", "state"),
    ("v_customers", "country"),
    ("v_customers", "region"),
    ("v_orders", "status"),
    ("v_orders", "currency"),
    ("v_shipments", "carrier"),
    ("v_shipments", "status"),
    ("v_payments", "method"),
    ("v_payments", "status"),
    ("v_support_tickets", "status"),
    ("v_marketing_campaigns", "channel"),
    ("v_campaign_events", "event_type"),
)


@dataclass(frozen=True)
class ResolvedValue:
    value: str
    method: str  # "exact" | "synonym" | "fuzzy"
    score: float  # 1.0 for exact/synonym; difflib ratio in [0, 1) for fuzzy


@dataclass(frozen=True)
class _Entry:
    relation: str
    column: str
    values: tuple[str, ...]  # actual distinct values present in the data


class ValueIndex:
    def __init__(self, entries: list[_Entry], synonyms: dict[str, dict[str, str]]) -> None:
        self._entries = {(e.relation, e.column): e for e in entries}
        # synonyms keyed "schema.view.column" in catalog.yml; re-key to
        # (view, column) to match resolve()'s input.
        self._synonyms: dict[tuple[str, str], dict[str, str]] = {}
        for qualified_key, mapping in synonyms.items():
            parts = qualified_key.split(".")
            view, column = parts[-2], parts[-1]
            self._synonyms[(view, column)] = {k.lower(): v for k, v in mapping.items()}

    def columns(self) -> list[tuple[str, str]]:
        return sorted(self._entries.keys())

    def resolve(self, relation: str, column: str, literal: str, limit: int = 3, cutoff: float = 0.6) -> list[ResolvedValue]:
        entry = self._entries.get((relation, column))
        if entry is None:
            return []

        needle = literal.strip()
        needle_lower = needle.lower()

        # 1. Exact match (case-insensitive).
        for v in entry.values:
            if v.lower() == needle_lower:
                return [ResolvedValue(v, "exact", 1.0)]

        # 2. Semantic synonym — solves the abbreviation case fuzzy matching
        #    can't.
        synonym_map = self._synonyms.get((relation, column))
        if synonym_map and needle_lower in synonym_map:
            mapped = synonym_map[needle_lower]
            if mapped in entry.values:
                return [ResolvedValue(mapped, "synonym", 1.0)]

        # 3. Fuzzy — typos/case variants. Skips values under
        #    MIN_FUZZY_TARGET_LENGTH: "use" vs "US" scores 0.8 on pure
        #    coincidence since both strings are short.
        scored = [
            (v, difflib.SequenceMatcher(None, needle_lower, v.lower()).ratio())
            for v in entry.values
            if len(v) >= MIN_FUZZY_TARGET_LENGTH
        ]
        scored = [(v, s) for v, s in scored if s >= cutoff]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [ResolvedValue(v, "fuzzy", round(s, 3)) for v, s in scored[:limit]]


def build(conn: "psycopg.Connection", synonyms: dict[str, dict[str, str]], schema: str = "analytics") -> ValueIndex:
    """Discover distinct values for every CANDIDATE_COLUMNS entry under
    MAX_DISTINCT_VALUES, via the trusted connection (never chatbot_ro).

    Reads the BASE tables, not the views: the tenant-scoped views embed
    `tenant_id = current_setting('app.tenant_id', true)` in their WHERE
    clause, so a connection that never sets that GUC gets zero rows back.
    Also the semantically correct scope regardless — "what values can a
    status column hold" is schema-level domain knowledge, not per-tenant.
    """
    entries: list[_Entry] = []
    with conn.cursor() as cur:
        for view, column in CANDIDATE_COLUMNS:
            source_table = view.removeprefix("v_")
            cur.execute(
                f'SELECT count(DISTINCT "{column}") FROM {schema}."{source_table}"'  # noqa: S608 — table/column from a fixed internal allowlist, not user input
            )
            (distinct_count,) = cur.fetchone()
            if distinct_count is None or distinct_count > MAX_DISTINCT_VALUES:
                continue
            cur.execute(
                f'SELECT DISTINCT "{column}" FROM {schema}."{source_table}" WHERE "{column}" IS NOT NULL ORDER BY 1'
            )
            values = tuple(row[0] for row in cur.fetchall())
            # Indexed under the VIEW name — the identifier the guard and
            # the generated SQL both use.
            entries.append(_Entry(relation=view, column=column, values=values))
    return ValueIndex(entries, synonyms)
