"""Execution-accuracy grading. Never string comparison: run both queries
for real, compare result sets — there are a dozen correct spellings of
any join, but only one set of rows a query that means the same thing
can return.
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
    """Whether row ORDER is part of the correct answer. Requires ORDER BY
    *combined with* LIMIT, not ORDER BY alone: a query can carry ORDER BY
    purely for a fixture's own readability with no real ordering in the
    question, and two independently-run GROUP BY queries with identical
    values can still return rows in different physical order — grading
    that as order-sensitive fails a genuinely correct answer. ORDER BY +
    LIMIT is different in kind: a different sort changes WHICH rows
    survive (a "top 5" query), not just their display order.
    """
    tree = parse_sql(sql)
    stmt = tree[0].stmt
    return bool(stmt.sortClause) and stmt.limitCount is not None


def _canonicalize_value(v):
    if isinstance(v, (int, float, Decimal)):
        return round(float(v), 2)
    return v


def canonicalize_row(row: tuple) -> tuple:
    # Sorted within the row — tolerates the model selecting columns in a
    # different order. Tradeoff: two different columns with the same
    # value set would also look equal.
    return tuple(sorted((_canonicalize_value(v) for v in row), key=str))


def results_match(gold_rows: list[tuple], candidate_rows: list[tuple], gold_sql: str) -> bool:
    """`gold_sql` is used only to decide whether row ORDER is part of the
    correct answer — see order_matters()."""
    if len(gold_rows) != len(candidate_rows):
        return False
    gold_canon = [canonicalize_row(r) for r in gold_rows]
    cand_canon = [canonicalize_row(r) for r in candidate_rows]
    if order_matters(gold_sql):
        return gold_canon == cand_canon
    return sorted(gold_canon, key=str) == sorted(cand_canon, key=str)
