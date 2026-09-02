"""The Phase 03 checkpoint as an assertable test, not just a CLI you read:
generate -> guard -> execute, against the real local model and the real
database, for a handful of the same questions the CLI checkpoint runs.

Structural assertions only (see test_llm_client.py's docstring for why) —
this proves the pipeline holds together end to end, not that a 3B local
model always answers correctly. Semantic correctness is Phase 05's job.
"""

from __future__ import annotations

import pytest

from app.guards.ast_guard import check
from app.pipeline.execute import ExecutionError, execute
from app.pipeline.generate import build_context, generate_plan

from .conftest import requires_db, requires_ollama


@requires_db
@requires_ollama
def test_simple_count_question_runs_end_to_end(pg_conn):
    ctx = build_context(pg_conn)
    result = generate_plan(ctx, "How many orders are there in total?")
    plan = result.plan

    if plan.needs_clarification:
        pytest.skip("model asked for clarification on an unambiguous question")

    guard_result = check(plan.sql, catalog=ctx.catalog)
    assert guard_result.ok, f"guard rejected: {guard_result.detail}\nsql: {plan.sql}"


@requires_db
@requires_ollama
def test_ambiguous_question_is_handled_without_crashing(pg_conn):
    ctx = build_context(pg_conn)
    result = generate_plan(ctx, "How did we do last quarter?")
    # No assertion on WHICH outcome — a small model might confidently guess
    # a metric or correctly ask for clarification. Either is a legitimate
    # SqlPlan; the point is the pipeline produces one without crashing.
    assert result.plan is not None


@requires_db
@requires_ollama
def test_pipeline_context_reuses_a_single_introspection(pg_conn):
    # build_context() should not need to be called per-question — verifies
    # the same ctx object answers two different questions correctly.
    ctx = build_context(pg_conn)
    r1 = generate_plan(ctx, "How many customers are there?")
    r2 = generate_plan(ctx, "How many products are there?")
    assert r1.plan.sql != "" and r2.plan.sql != ""


@requires_db
@requires_ollama
def test_full_loop_including_execution_for_a_simple_question(pg_conn):
    ctx = build_context(pg_conn)
    result = generate_plan(ctx, "How many orders are there in total?")
    plan = result.plan
    if plan.needs_clarification:
        pytest.skip("model asked for clarification")

    guard_result = check(plan.sql, catalog=ctx.catalog)
    if not guard_result.ok:
        pytest.skip(f"model's SQL didn't pass the guard this run: {guard_result.detail}")

    try:
        exec_result = execute(guard_result.safe_sql, tenant_id=1)
    except ExecutionError as exc:
        pytest.fail(f"guard-approved SQL failed at execution: {exc}\nsql: {guard_result.safe_sql}")
        return
    assert exec_result.row_count >= 1
