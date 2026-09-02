"""The value index against live seeded data — proves the actual claim the
project plan makes: 'California' resolves to 'CA', and it proves that
claim via the synonym map, not fuzzy string matching (which cannot make
that connection — see value_index.py's module docstring).
"""

from __future__ import annotations

from app.schema import semantic, value_index

from .conftest import requires_db


@requires_db
def test_exact_match_needs_no_resolution(pg_conn):
    idx = value_index.build(pg_conn, semantic.value_synonyms())
    results = idx.resolve("v_orders", "status", "shipped")
    assert results[0].method == "exact"
    assert results[0].value == "shipped"


@requires_db
def test_california_resolves_to_ca_via_synonym_not_fuzzy_match(pg_conn):
    idx = value_index.build(pg_conn, semantic.value_synonyms())
    results = idx.resolve("v_customers", "state", "California")
    assert results, "no resolution found for 'California'"
    assert results[0].value == "CA"
    assert results[0].method == "synonym"


@requires_db
def test_fuzzy_matching_alone_cannot_find_california_to_ca(pg_conn):
    # The negative control that justifies the synonym map's existence:
    # confirm plain trigram/edit-distance similarity between "California"
    # and "CA" really is below any reasonable cutoff, so a reader can't
    # dismiss the synonym map as unnecessary belt-and-suspenders.
    import difflib

    ratio = difflib.SequenceMatcher(None, "california", "ca").ratio()
    assert ratio < 0.4, f"expected low similarity, got {ratio}"


@requires_db
def test_typo_resolves_via_fuzzy_match(pg_conn):
    idx = value_index.build(pg_conn, semantic.value_synonyms())
    results = idx.resolve("v_orders", "status", "shiped")  # missing a 'p'
    assert results, "no fuzzy match found for a one-letter typo"
    assert results[0].value == "shipped"
    assert results[0].method == "fuzzy"
    assert 0 < results[0].score < 1.0


@requires_db
def test_case_variant_resolves_via_fuzzy_or_exact(pg_conn):
    idx = value_index.build(pg_conn, semantic.value_synonyms())
    results = idx.resolve("v_orders", "status", "SHIPPED")
    assert results
    assert results[0].value == "shipped"


@requires_db
def test_unresolvable_literal_returns_empty(pg_conn):
    idx = value_index.build(pg_conn, semantic.value_synonyms())
    results = idx.resolve("v_orders", "status", "quantum-teleported")
    assert results == []


@requires_db
def test_high_cardinality_column_is_not_indexed(pg_conn):
    # customer email/name are not in CANDIDATE_COLUMNS — resolving against
    # them should come back empty rather than silently no-op-matching.
    idx = value_index.build(pg_conn, semantic.value_synonyms())
    assert ("v_customers", "email") not in idx.columns()
    assert idx.resolve("v_customers", "email", "anything") == []


@requires_db
def test_candidate_columns_all_stayed_under_the_cardinality_cap(pg_conn):
    # If a future schema change pushed one of these past MAX_DISTINCT_VALUES
    # (e.g. adding many more carriers), it would silently drop out of the
    # index — worth a loud assertion here rather than a quiet gap.
    idx = value_index.build(pg_conn, semantic.value_synonyms())
    assert set(idx.columns()) == set(value_index.CANDIDATE_COLUMNS)
