"""Value-hint extraction against the real, live value index — the actual
end-to-end proof of the "revenue in California" fix, from raw question
text this time, not a direct resolve() call (see test_value_index.py for
that lower-level version).
"""

from __future__ import annotations

from app.pipeline.hints import extract_value_hints
from app.schema import semantic, value_index

from .conftest import requires_db


@requires_db
def test_california_in_a_real_question_produces_a_synonym_hint(pg_conn):
    idx = value_index.build(pg_conn, semantic.value_synonyms())
    hints = extract_value_hints("How many customers are in California?", idx)
    assert any("v_customers.state = 'CA'" in h and "synonym" in h for h in hints)


@requires_db
def test_typo_in_a_real_question_produces_a_fuzzy_hint(pg_conn):
    idx = value_index.build(pg_conn, semantic.value_synonyms())
    hints = extract_value_hints("How many orders had a status of shiped?", idx)
    assert any("v_orders.status = 'shipped'" in h and "fuzzy" in h for h in hints)


@requires_db
def test_question_with_no_resolvable_literal_produces_no_hints(pg_conn):
    idx = value_index.build(pg_conn, semantic.value_synonyms())
    hints = extract_value_hints("How many orders are there in total?", idx)
    assert hints == []


@requires_db
def test_exact_value_already_correct_produces_no_noise_hint(pg_conn):
    # "shipped" already matches the real column value verbatim — hinting
    # it anyway would just be prompt noise the model has to read past.
    idx = value_index.build(pg_conn, semantic.value_synonyms())
    hints = extract_value_hints("How many orders were shipped?", idx)
    assert hints == []
