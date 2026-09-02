"""Golden/ambiguity/adversarial set integrity — not grading a model, just
confirming the eval fixtures themselves are still valid against the
current schema and data. Belongs in the regular pytest suite (not just
eval/run_eval.py) so a schema change that silently breaks a golden case
is caught the same way any other regression is, not only when someone
remembers to run the eval harness by hand.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.guards import ast_guard
from app.pipeline.execute import ExecutionError, execute
from app.schema.catalog import SnapshotCatalog
from app.schema.introspect import introspect

from .conftest import requires_db

EVAL_DIR = Path(__file__).resolve().parents[2] / "eval"


def _load_golden():
    cases = []
    for tier in ("easy", "medium", "hard"):
        with open(EVAL_DIR / "golden" / f"{tier}.yml", encoding="utf-8") as f:
            cases.extend(yaml.safe_load(f))
    return cases


def _load_ambiguity():
    with open(EVAL_DIR / "ambiguity" / "cases.yml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_golden_yaml_files_are_well_formed():
    cases = _load_golden()
    assert len(cases) >= 40
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate golden case id"
    for c in cases:
        assert {"id", "question", "sql", "difficulty"} <= set(c.keys())
        assert c["difficulty"] in ("easy", "medium", "hard")
        assert c["question"].strip()
        assert c["sql"].strip()


def test_ambiguity_yaml_is_well_formed_and_balanced():
    cases = _load_ambiguity()
    assert len(cases) >= 20
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate ambiguity case id"
    should_clarify = sum(1 for c in cases if c["should_clarify"])
    should_not = len(cases) - should_clarify
    # Not a strict 50/50 requirement, but neither bucket should be empty —
    # an all-ambiguous or all-clear set can't measure both recall and the
    # false-positive rate.
    assert should_clarify > 0
    assert should_not > 0


@requires_db
def test_every_golden_case_still_passes_the_guard(pg_conn):
    catalog = SnapshotCatalog(introspect(pg_conn))
    failures = []
    for case in _load_golden():
        result = ast_guard.check(case["sql"], catalog=catalog)
        if not result.ok:
            failures.append(f"{case['id']}: {result.reason} {result.detail}")
    assert not failures, "golden case(s) no longer pass the guard:\n" + "\n".join(failures)


@requires_db
def test_every_golden_case_still_executes(pg_conn):
    failures = []
    for case in _load_golden():
        try:
            execute(case["sql"], tenant_id=1)
        except ExecutionError as exc:
            failures.append(f"{case['id']}: {exc}")
    assert not failures, "golden case(s) no longer execute against live data:\n" + "\n".join(failures)


@requires_db
def test_no_golden_case_references_the_legacy_table():
    # A golden case that accidentally queried legacy_orders_flat would be
    # un-runnable by chatbot_ro by construction (no grant) — this is a
    # cheaper, more specific check that catches the mistake by intent,
    # not just by the execution failure it would eventually cause.
    for case in _load_golden():
        assert "legacy_orders_flat" not in case["sql"].lower(), case["id"]
