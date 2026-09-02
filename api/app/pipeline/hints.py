"""Extracts candidate literals from a user's question and resolves them
through the value index (schema/value_index.py) before generation — the
concrete fix for "revenue in California" silently returning zero rows
because the column stores 'CA'.

Extraction covers three shapes: quoted substrings, Title-Case phrases
(proper nouns — place names), and individual lowercase words (typos:
"shiped" for "shipped"). The first pass only caught the first two, which
made "How many orders had a status of shiped?" produce nothing — a typo
is exactly the case fuzzy matching exists for, and it's usually typed in
lowercase, not capitalized. A stopword list plus MIN_FUZZY_SCORE keep the
lowercase pass from turning every query word into a resolution attempt.
"""

from __future__ import annotations

import re

from ..schema.value_index import ValueIndex

_QUOTED_RE = re.compile(r"""['"]([^'"]{2,40})['"]""")
_TITLE_CASE_RE = re.compile(r"\b[A-Z][a-zA-Z]*(?:\s+[A-Z][a-zA-Z]*){0,2}\b")
_WORD_RE = re.compile(r"[a-z]{3,}")

# Common words in an analytics question that would otherwise be tried
# against every indexed column — not an exhaustive stopword list, just
# the ones that actually showed up as noise during testing.
_STOPWORDS = frozenset(
    """
    how many much what which when where who why show tell give list find
    are was were is has have had did does the a an in on at of to for and
    or that this these those last next this month year quarter week day
    total count all each per by with from most top average avg sum per
    orders order customer customers product products revenue amount rate
    number over under between since ago recent still open each was were
    """.split()
)


#: Below this, a fuzzy hit is more likely noise than signal — an
#: unresolved literal is a better outcome than a wrong "correction".
MIN_FUZZY_SCORE = 0.75


def _candidate_phrases(question: str) -> list[str]:
    phrases = list(_QUOTED_RE.findall(question))
    phrases.extend(_TITLE_CASE_RE.findall(question))
    # Drop the sentence-initial capitalized word unless it's multi-word
    # (Title Case phrase) — "What were sales..." shouldn't try to resolve
    # "What" against every indexed column.
    first_word = question.strip().split(" ", 1)[0] if question.strip() else ""
    phrases = [p for p in phrases if p != first_word or " " in p]

    lowercase_words = [w for w in _WORD_RE.findall(question.lower()) if w not in _STOPWORDS]
    phrases.extend(lowercase_words)

    return list(dict.fromkeys(phrases))


def extract_value_hints(question: str, index: ValueIndex) -> list[str]:
    hints: list[str] = []
    seen: set[str] = set()
    for phrase in _candidate_phrases(question):
        for relation, column in index.columns():
            results = index.resolve(relation, column, phrase)
            if not results:
                continue
            top = results[0]
            if top.method == "exact":
                continue  # already the real value, nothing to hint
            if top.method == "fuzzy" and top.score < MIN_FUZZY_SCORE:
                continue
            key = f"{relation}.{column}={top.value}"
            if key in seen:
                continue
            seen.add(key)
            hints.append(f'"{phrase}" -> {relation}.{column} = \'{top.value}\' (resolved via {top.method})')
    return hints
