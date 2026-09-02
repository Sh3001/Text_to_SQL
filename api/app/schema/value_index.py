"""The value index — resolves a literal in a user's question to the actual
value stored in a low-cardinality column, before that literal ever reaches
the model's generated SQL. Fixes the single largest source of zero-row
results in the plan's own accounting: `region = 'California'` when the
column stores `'CA'`.

Two genuinely different problems live here, and conflating them was an
early mistake worth documenting rather than erasing:

  * TYPOS / CASING ("califronia" -> "california", "Shipped" -> "shipped")
    are a nearby-string problem — edit-distance / trigram similarity
    solves these because the strings genuinely overlap.
  * ABBREVIATIONS ("California" -> "CA", "United States" -> "US") are NOT
    a nearby-string problem — "California" and "CA" share almost no
    substring overlap, so no similarity metric finds them close. This
    needs an explicit lookup, sourced from semantic/catalog.yml's
    `value_synonyms` block, maintained by a human the same way the rest
    of the semantic layer is.

`resolve()` tries synonyms first (exact semantic knowledge beats a guess),
then falls back to fuzzy matching against the real distinct values in the
column. Discovery (which columns qualify, what values they hold) is a
one-time/scheduled DB read using the pipeline's own trusted connection —
same trust boundary as catalog introspection, never chatbot_ro. Matching
itself is pure Python (difflib) against an in-memory snapshot, so a
request-time lookup costs no round trip.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psycopg

#: A column qualifies for the index if it has at most this many distinct
#: values — comfortably "a category", not "free text". Loaded once at
#: build time via a real COUNT(DISTINCT ...), not guessed from the type.
MAX_DISTINCT_VALUES = 500

#: Values shorter than this are never fuzzy-matched — see the comment in
#: resolve() for why (edit-distance similarity is unreliable at this length).
MIN_FUZZY_TARGET_LENGTH = 4

#: (view, column) candidates. Kept as an explicit list rather than
#: auto-discovering every text column in every view: a text column with
#: high cardinality (customer name, review body) would pass a naive
#: distinct-count check on a small dataset and stop being useful at real
#: scale, and hand-picking the columns that are genuinely categorical is
#: cheap to maintain and impossible to get silently wrong.
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
        # (view, column) with lowercase phrase keys to match resolve()'s input.
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

        # 1. Exact match (case-insensitive) against real data — the common
        #    case, and worth confirming even though it needed no resolution.
        for v in entry.values:
            if v.lower() == needle_lower:
                return [ResolvedValue(v, "exact", 1.0)]

        # 2. Semantic synonym — solves the abbreviation case fuzzy matching
        #    structurally cannot.
        synonym_map = self._synonyms.get((relation, column))
        if synonym_map and needle_lower in synonym_map:
            mapped = synonym_map[needle_lower]
            if mapped in entry.values:
                return [ResolvedValue(mapped, "synonym", 1.0)]

        # 3. Fuzzy — typos and case variants where the strings genuinely
        #    overlap. Deliberately does NOT run when the synonym map
        #    matched above; a confirmed synonym is not a guess. Skips
        #    values shorter than MIN_FUZZY_TARGET_LENGTH: edit-distance
        #    similarity is close to meaningless on 2-3 character codes —
        #    "use" vs "US" scores 0.8, well past a naive 0.6-0.75 cutoff,
        #    purely because both strings are short (found by testing
        #    against real questions, not a hypothetical). Short codes
        #    ('US', 'CA', 'GB') already have exact matching and, for the
        #    cases that need it, the explicit synonym map — they were
        #    never fuzzy matching's job.
        scored = [
            (v, difflib.SequenceMatcher(None, needle_lower, v.lower()).ratio())
            for v in entry.values
            if len(v) >= MIN_FUZZY_TARGET_LENGTH
        ]
        scored = [(v, s) for v, s in scored if s >= cutoff]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [ResolvedValue(v, "fuzzy", round(s, 3)) for v, s in scored[:limit]]


def build(conn: "psycopg.Connection", synonyms: dict[str, dict[str, str]], schema: str = "analytics") -> ValueIndex:
    """Discover distinct values for every CANDIDATE_COLUMNS entry that
    actually stays under MAX_DISTINCT_VALUES, using the pipeline's trusted
    connection (never chatbot_ro — same boundary as introspect.introspect).

    Reads the underlying BASE tables, not the chatbot-facing views — same
    naming-convention inference introspect.py uses for PK/FK (`v_orders` ->
    `orders`). Two reasons, not one: first, the tenant-scoped views
    (db/00_schema.sql) embed `tenant_id = current_setting('app.tenant_id',
    true)` directly in their WHERE clause, so querying them from a
    connection that never sets that GUC — which this one deliberately
    doesn't, since it isn't serving any one tenant's request — silently
    returns zero rows (found the hard way: every tenant-scoped candidate
    column came back empty on the first pass). Second, and more
    fundamentally, "what values can a status/currency/carrier column hold"
    is schema-level domain knowledge, not a fact that varies per tenant —
    reading across all tenants from the base table is the semantically
    correct scope for this index, not merely a workaround for the first
    problem.
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
            # Indexed under the VIEW name — that's the identifier the guard
            # and the generated SQL both use; source_table was only needed
            # to reach past the tenant filter above.
            entries.append(_Entry(relation=view, column=column, values=values))
    return ValueIndex(entries, synonyms)
