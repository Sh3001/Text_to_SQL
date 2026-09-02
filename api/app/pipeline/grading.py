"""Execution-accuracy grading — split out from eval/run_eval.py so it's a
real, importable, unit-testable module (api/tests/test_grading.py) rather
than logic trapped inside a standalone script. Grading is execution
accuracy, never string comparison: run both queries for real, compare
result sets. There are a dozen correct spellings of any join; there's
only one set of rows a query that means the same thing can return.
"""

from __future__ import annotations

from decimal import Decimal

from pglast import parse_sql


def has_order_by(sql: str) -> bool:
    """Parsed, not string-searched — `ORDER BY` could appear inside a
    string literal or a column named `order_by` and a text search would
    misfire either way.
    """
    tree = parse_sql(sql)
    return bool(tree[0].stmt.sortClause)


def order_matters(sql: str) -> bool:
    """Whether row ORDER is part of the correct answer, not just how the
    query happened to be written. Requires ORDER BY *combined with*
    LIMIT — not ORDER BY alone.

    Found necessary by testing, not designed in from the start: a golden
    case like "orders_by_status" carries `ORDER BY status` purely so the
    authored fixture reads predictably — the actual question ("how many
    orders per status") has no notion of a correct row order at all.
    Grading that as order-sensitive meant two independently-executed
    GROUP BY queries returning the exact same values could still fail
    the comparison purely because Postgres's own hash-aggregate chose a
    different physical row order — confirmed directly: gold and
    candidate rows for a real case were byte-identical when sorted, and
    `results_match` still returned False. 20 of this project's 52 golden
    cases had `ORDER BY` without `LIMIT` and were silently graded wrong
    by this bug before it was found.

    ORDER BY + LIMIT is different in kind: it doesn't just display rows
    in an order, it determines WHICH rows survive at all (a "top 5"
    query with a different sort produces a different 5 rows) — that's
    the case order-sensitive comparison exists for.
    """
    tree = parse_sql(sql)
    stmt = tree[0].stmt
    return bool(stmt.sortClause) and stmt.limitCount is not None


def _canonicalize_value(v):
    if isinstance(v, (int, float, Decimal)):
        return round(float(v), 2)
    return v


def canonicalize_row(row: tuple) -> tuple:
    # Sorted within the row — tolerates the model selecting the same
    # columns in a different order (`region, revenue` vs `revenue,
    # region`), which a small local model does often enough that
    # comparing raw tuples position-by-position would fail a genuinely
    # correct answer for a superficial reason. Real tradeoff, not free:
    # two different columns that happen to hold the same value set would
    # look equal too — documented, not hidden.
    return tuple(sorted((_canonicalize_value(v) for v in row), key=str))


def results_match(gold_rows: list[tuple], candidate_rows: list[tuple], gold_sql: str) -> bool:
    """`gold_sql` is used only to decide whether row ORDER is part of the
    correct answer — see order_matters() for exactly what that means and
    why "the gold query has an ORDER BY" alone isn't the right signal.
    """
    if len(gold_rows) != len(candidate_rows):
        return False
    gold_canon = [canonicalize_row(r) for r in gold_rows]
    cand_canon = [canonicalize_row(r) for r in candidate_rows]
    if order_matters(gold_sql):
        return gold_canon == cand_canon
    return sorted(gold_canon, key=str) == sorted(cand_canon, key=str)
