"""Execution-accuracy grading — no DB, no model, pure logic. The live
end-to-end proof (self-consistency + a deliberately wrong variant) lives
in eval/run_eval.py's own smoke test, run against real data; these are
the fast, always-on unit tests for the comparison rules themselves.
"""

from __future__ import annotations

from decimal import Decimal

from app.pipeline.grading import canonicalize_row, has_order_by, order_matters, results_match


def test_identical_rows_match():
    assert results_match([(1, "a"), (2, "b")], [(1, "a"), (2, "b")], "SELECT 1")


def test_different_row_count_never_matches():
    assert not results_match([(1,)], [(1,), (2,)], "SELECT 1")


def test_missing_row_is_a_mismatch():
    gold = [("cancelled", 100), ("shipped", 200)]
    candidate = [("cancelled", 100)]  # dropped a status entirely
    assert not results_match(gold, candidate, "SELECT status, count(*) FROM x")


def test_wrong_value_is_a_mismatch():
    gold = [("shipped", 200)]
    candidate = [("shipped", 199)]
    assert not results_match(gold, candidate, "SELECT status, count(*) FROM x")


def test_row_order_insensitive_when_gold_has_no_order_by():
    gold = [("a", 1), ("b", 2)]
    candidate = [("b", 2), ("a", 1)]
    assert results_match(gold, candidate, "SELECT x, y FROM t GROUP BY x")


def test_row_order_insensitive_even_with_order_by_if_there_is_no_limit():
    # Regression test for a real bug: a golden case like "orders per
    # status" carries ORDER BY purely so the fixture reads predictably —
    # the question itself has no correct row order. Two independently
    # -run GROUP BY queries returning identical values can still come
    # back in a different physical row order (confirmed directly against
    # live data: a real case's gold and candidate rows were
    # byte-identical when sorted, and the old rule — order-sensitive
    # whenever ORDER BY is present at all — still failed it). 20 of this
    # project's 52 golden cases hit exactly this shape.
    gold = [("a", 2), ("b", 1)]
    reordered = [("b", 1), ("a", 2)]
    assert results_match(gold, reordered, "SELECT x, y FROM t GROUP BY x ORDER BY y DESC")


def test_row_order_matters_when_order_by_is_paired_with_limit():
    # ORDER BY + LIMIT is the case that's actually order-sensitive: a
    # "top 5" query with a different sort produces a different 5 rows,
    # not just the same rows in a different sequence.
    gold = [("a", 2), ("b", 1)]
    reordered = [("b", 1), ("a", 2)]
    sql = "SELECT x, y FROM t ORDER BY y DESC LIMIT 2"
    assert not results_match(gold, reordered, sql)
    assert results_match(gold, gold, sql)


def test_order_matters_requires_both_order_by_and_limit():
    assert not order_matters("SELECT * FROM t ORDER BY x")
    assert not order_matters("SELECT * FROM t LIMIT 5")
    assert order_matters("SELECT * FROM t ORDER BY x LIMIT 5")


def test_column_order_within_a_row_is_tolerated():
    # A model selecting `count(*), status` instead of `status, count(*)`
    # should still be graded correct — same data, different column order.
    gold = [("shipped", 200)]
    candidate = [(200, "shipped")]
    assert results_match(gold, candidate, "SELECT status, count(*) FROM x")


def test_numeric_types_compare_by_value_not_python_type():
    # Decimal vs float vs int for "the same number" must all compare equal
    # — both sides go through the same execute() pipeline but a differently
    # -cast generated query could come back as a different Python type.
    gold = [(Decimal("42.00"),)]
    candidate = [(42.0,)]
    assert results_match(gold, candidate, "SELECT sum(x) FROM t")

    gold2 = [(Decimal("42"),)]
    candidate2 = [(42,)]
    assert results_match(gold2, candidate2, "SELECT count(*) FROM t")


def test_has_order_by_detects_real_sort_clause():
    assert has_order_by("SELECT * FROM t ORDER BY x")
    assert not has_order_by("SELECT * FROM t WHERE x = 1")


def test_has_order_by_is_not_fooled_by_the_words_in_a_string_literal():
    # The whole point of parsing instead of searching: a literal
    # containing the words "order by" must not be mistaken for a real
    # ORDER BY clause.
    assert not has_order_by("SELECT * FROM t WHERE note = 'please order by tomorrow'")


def test_canonicalize_row_sorts_by_string_representation():
    assert canonicalize_row((2, 1)) == canonicalize_row((1, 2))
