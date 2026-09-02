"""The Phase 04 checkpoint, made literal: every row of the project plan's
error taxonomy gets a test that provokes it and asserts the recovery.

These tests stub only the LLM call (via monkeypatch, replacing
answer.generate_plan with a canned sequence of results/exceptions) — the
guard, the plan-budget check, execution, and the zero-row diagnostic are
all real, running against the live database. That's deliberate: a 3B
local model's exact wording isn't controllable enough to reliably provoke
every failure class on demand (see test_pipeline_end_to_end.py for the
tests that DO run the real model), but the repair loop's *handling* of
each failure is exactly what Phase 04 needs proven, and that doesn't
depend on the model being cooperative.
"""

from __future__ import annotations

from app.guards.errors import RejectReason
from app.llm.client import ModelUnavailableError, OutputParseError
from app.llm.client import GenerationResult as _GenerationResult
from app.llm.schemas import SqlPlan
from app.pipeline import answer as answer_module
from app.pipeline.errors import Action, FailureKind

from .conftest import requires_db


def _gen_result(sql: str | None = None, clarifying_question: str | None = None, confidence: str = "high") -> _GenerationResult:
    plan = SqlPlan(
        intent="test",
        sql=sql or "SELECT 1 WHERE false",
        confidence=confidence,
        clarifying_question=clarifying_question,
    )
    return _GenerationResult(plan=plan, model="fake", prompt_eval_count=1, eval_count=1, total_duration_ms=1.0)


def _stub_sequence(monkeypatch, *results_or_exceptions):
    """Each call to answer_module.generate_plan returns/raises the next
    item; extra calls beyond the list repeat the last item (so a test
    doesn't need to enumerate every possible extra retry).
    """
    calls = {"n": 0}

    def fake_generate_plan(ctx, question, repair_attempts=None, model=None):
        i = min(calls["n"], len(results_or_exceptions) - 1)
        calls["n"] += 1
        item = results_or_exceptions[i]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(answer_module, "generate_plan", fake_generate_plan)
    return calls


# ---------------------------------------------------------------------------
# Row: Ambiguous question -> ASK
# ---------------------------------------------------------------------------

@requires_db
def test_ambiguous_question_asks_instead_of_guessing(pg_conn, monkeypatch):
    from app.pipeline.generate import build_context

    ctx = build_context(pg_conn)
    _stub_sequence(monkeypatch, _gen_result(clarifying_question="Which metric do you mean?", confidence="low"))

    outcome = answer_module.answer(ctx, "How did we do?", tenant_id=1)
    assert outcome.verdict is Action.ASK
    assert outcome.failure_kind is FailureKind.AMBIGUOUS
    assert outcome.message == "Which metric do you mean?"
    assert outcome.repair_attempts_used == 0  # ambiguity is not a repair-loop case


# ---------------------------------------------------------------------------
# Row: Unknown table/column -> REPAIR -> recovers
# ---------------------------------------------------------------------------

@requires_db
def test_unknown_table_is_repaired_with_the_real_error_fed_back(pg_conn, monkeypatch):
    from app.pipeline.generate import build_context

    ctx = build_context(pg_conn)
    calls = _stub_sequence(
        monkeypatch,
        _gen_result(sql="SELECT id FROM analytics.v_order"),  # typo: singular
        _gen_result(sql="SELECT id FROM analytics.v_orders LIMIT 1"),  # corrected on retry
    )

    outcome = answer_module.answer(ctx, "How many orders?", tenant_id=1)
    assert outcome.verdict is Action.ANSWERED
    assert outcome.repair_attempts_used == 1
    assert calls["n"] == 2


@requires_db
def test_unknown_table_never_recovering_gives_up_after_budget(pg_conn, monkeypatch):
    from app.pipeline.generate import build_context

    ctx = build_context(pg_conn)
    _stub_sequence(monkeypatch, _gen_result(sql="SELECT id FROM analytics.v_nonexistent"))  # always wrong

    outcome = answer_module.answer(ctx, "How many widgets?", tenant_id=1)
    assert outcome.verdict is Action.GIVE_UP
    assert outcome.failure_kind is FailureKind.UNKNOWN_IDENTIFIER
    assert outcome.repair_attempts_used == answer_module.MAX_REPAIR_ATTEMPTS


# ---------------------------------------------------------------------------
# Row: Syntax error -> REPAIR -> recovers
# ---------------------------------------------------------------------------

@requires_db
def test_guard_level_syntax_error_is_repaired(pg_conn, monkeypatch):
    from app.pipeline.generate import build_context

    ctx = build_context(pg_conn)
    _stub_sequence(
        monkeypatch,
        _gen_result(sql="SELEC id FROM analytics.v_orders"),  # typo'd keyword
        _gen_result(sql="SELECT id FROM analytics.v_orders LIMIT 1"),
    )

    outcome = answer_module.answer(ctx, "ids please", tenant_id=1)
    assert outcome.verdict is Action.ANSWERED
    assert outcome.repair_attempts_used == 1


# ---------------------------------------------------------------------------
# Row: Unsafe statement -> BLOCK, terminal, never repaired
# ---------------------------------------------------------------------------

@requires_db
def test_unsafe_statement_is_blocked_not_repaired(pg_conn, monkeypatch):
    from app.pipeline.generate import build_context

    ctx = build_context(pg_conn)
    calls = _stub_sequence(monkeypatch, _gen_result(sql="DROP TABLE analytics.orders"))

    outcome = answer_module.answer(ctx, "delete everything", tenant_id=1)
    assert outcome.verdict is Action.BLOCK
    assert outcome.failure_kind is FailureKind.UNSAFE
    assert outcome.repair_attempts_used == 0
    assert calls["n"] == 1  # never retried — the whole point of "terminal"


# ---------------------------------------------------------------------------
# Row: Over plan budget -> REPAIR -> recovers
# ---------------------------------------------------------------------------

@requires_db
def test_over_budget_query_is_repaired(pg_conn, monkeypatch):
    from app.pipeline.generate import build_context

    ctx = build_context(pg_conn)
    _stub_sequence(
        monkeypatch,
        # An aggregate, not a bare SELECT * — confirmed by testing that a
        # bare cartesian `SELECT * FROM v_orders, v_order_items` is NOT
        # actually over budget once the guard injects LIMIT 1000: Postgres
        # can produce the first 1000 rows of an unfiltered cross join
        # cheaply (a few nested-loop iterations), so the cost estimate is
        # genuinely low — the LIMIT already neutralizes that shape of
        # disaster (see plan_budget.py's module docstring). An aggregate
        # forces full materialization before producing any output row, so
        # LIMIT can't rescue it — that's what actually needs this layer.
        _gen_result(sql="SELECT count(*) FROM analytics.v_orders, analytics.v_order_items"),
        _gen_result(sql="SELECT count(*) FROM analytics.v_orders"),  # narrow, cheap
    )

    outcome = answer_module.answer(ctx, "show me everything", tenant_id=1)
    assert outcome.verdict is Action.ANSWERED
    assert outcome.repair_attempts_used == 1


@requires_db
def test_over_budget_query_that_never_narrows_gives_up(pg_conn, monkeypatch):
    from app.pipeline.generate import build_context

    ctx = build_context(pg_conn)
    _stub_sequence(monkeypatch, _gen_result(sql="SELECT count(*) FROM analytics.v_orders, analytics.v_order_items"))

    outcome = answer_module.answer(ctx, "show me everything", tenant_id=1)
    assert outcome.verdict is Action.GIVE_UP
    assert outcome.failure_kind is FailureKind.OVER_BUDGET
    assert outcome.repair_attempts_used == answer_module.MAX_REPAIR_ATTEMPTS


@requires_db
def test_limit_alone_does_not_defeat_the_budget_check_for_aggregates(pg_conn):
    # The regression test for the discovery above: a plain unfiltered
    # cross join WITH the guard's LIMIT already applied is cheap (correct
    # — LIMIT really does make it cheap), but the same join wrapped in an
    # aggregate stays expensive regardless of LIMIT. Both facts, asserted
    # directly against plan_budget.check(), not just inferred from the
    # repair-loop tests above.
    from app.pipeline import plan_budget

    cheap = plan_budget.check("SELECT * FROM analytics.v_orders, analytics.v_order_items LIMIT 1000", tenant_id=1)
    assert cheap.ok is True

    expensive = plan_budget.check("SELECT count(*) FROM analytics.v_orders, analytics.v_order_items", tenant_id=1)
    assert expensive.ok is False


# ---------------------------------------------------------------------------
# Row: Timeout -> ASK, never silently retried
# ---------------------------------------------------------------------------

@requires_db
def test_timeout_asks_and_does_not_retry(pg_conn, monkeypatch):
    from app.pipeline.execute import ExecutionError
    from app.pipeline.generate import build_context

    ctx = build_context(pg_conn)
    calls = _stub_sequence(monkeypatch, _gen_result(sql="SELECT count(*) FROM analytics.v_orders"))

    def fake_execute(safe_sql, tenant_id, **kwargs):
        raise ExecutionError("canceling statement due to statement timeout", sqlstate="57014")

    monkeypatch.setattr(answer_module, "execute", fake_execute)

    outcome = answer_module.answer(ctx, "how many orders", tenant_id=1)
    assert outcome.verdict is Action.ASK
    assert outcome.failure_kind is FailureKind.TIMEOUT
    assert "timed out" in outcome.message
    assert calls["n"] == 1  # a timeout is never silently retried


@requires_db
def test_execute_itself_raises_the_real_sqlstate_on_a_genuine_timeout(pg_conn):
    # The lower-level proof the mechanism is real, not just mocked: an
    # actual query against actual data, forced to time out via a real
    # (tiny) statement_timeout override — see execute()'s docstring for
    # why this needs set_config rather than a bind-parameterized SET.
    from app.pipeline.errors import classify_execution_error
    from app.pipeline.execute import ExecutionError, execute

    try:
        execute("SELECT * FROM analytics.v_order_items", tenant_id=1, statement_timeout_ms=1)
        assert False, "expected a real timeout"
    except ExecutionError as exc:
        assert exc.sqlstate == "57014"
        kind, action = classify_execution_error(exc)
        assert kind is FailureKind.TIMEOUT
        assert action is Action.ASK


# ---------------------------------------------------------------------------
# Row: generic execution error -> REPAIR
# ---------------------------------------------------------------------------

@requires_db
def test_generic_execution_error_is_repaired(pg_conn, monkeypatch):
    from app.pipeline.generate import build_context

    ctx = build_context(pg_conn)
    _stub_sequence(
        monkeypatch,
        _gen_result(sql="SELECT 1/0"),  # valid syntax, real Postgres error at execution: division by zero
        _gen_result(sql="SELECT count(*) FROM analytics.v_orders"),
    )

    outcome = answer_module.answer(ctx, "a broken query", tenant_id=1)
    assert outcome.verdict is Action.ANSWERED
    assert outcome.repair_attempts_used == 1


# ---------------------------------------------------------------------------
# Row: Zero rows -> DIAGNOSE
# ---------------------------------------------------------------------------

@requires_db
def test_zero_rows_triggers_diagnosis_naming_the_culprit_predicate(pg_conn, monkeypatch):
    from app.pipeline.generate import build_context

    ctx = build_context(pg_conn)
    # 'Shipped' (capitalized) doesn't match real lowercase seeded data —
    # guaranteed zero rows, and a real, known culprit predicate.
    _stub_sequence(monkeypatch, _gen_result(sql="SELECT * FROM analytics.v_orders WHERE status = 'Shipped'"))

    outcome = answer_module.answer(ctx, "shipped orders", tenant_id=1)
    assert outcome.verdict is Action.DIAGNOSE
    assert outcome.failure_kind is FailureKind.ZERO_ROWS
    assert outcome.diagnosis is not None
    assert outcome.diagnosis.culprit == "status = 'Shipped'"
    assert outcome.diagnosis.baseline_row_count > 0


# ---------------------------------------------------------------------------
# Row: Model unavailable -> GIVE_UP, no retry
# ---------------------------------------------------------------------------

@requires_db
def test_model_unavailable_gives_up_without_retrying(pg_conn, monkeypatch):
    from app.pipeline.generate import build_context

    ctx = build_context(pg_conn)
    calls = _stub_sequence(monkeypatch, ModelUnavailableError("Ollama server unreachable"))

    outcome = answer_module.answer(ctx, "anything", tenant_id=1)
    assert outcome.verdict is Action.GIVE_UP
    assert outcome.failure_kind is FailureKind.MODEL_UNAVAILABLE
    assert calls["n"] == 1  # an infra failure isn't worth retrying the same request


# ---------------------------------------------------------------------------
# Row: Model output doesn't parse -> REPAIR -> recovers
# ---------------------------------------------------------------------------

@requires_db
def test_unparseable_model_output_is_repaired(pg_conn, monkeypatch):
    from app.pipeline.generate import build_context

    ctx = build_context(pg_conn)
    _stub_sequence(
        monkeypatch,
        OutputParseError("model output didn't validate as SqlPlan"),
        _gen_result(sql="SELECT count(*) FROM analytics.v_orders"),
    )

    outcome = answer_module.answer(ctx, "anything", tenant_id=1)
    assert outcome.verdict is Action.ANSWERED
    assert outcome.repair_attempts_used == 1


@requires_db
def test_persistently_unparseable_output_gives_up(pg_conn, monkeypatch):
    from app.pipeline.generate import build_context

    ctx = build_context(pg_conn)
    _stub_sequence(monkeypatch, OutputParseError("never valid"))

    outcome = answer_module.answer(ctx, "anything", tenant_id=1)
    assert outcome.verdict is Action.GIVE_UP
    assert outcome.failure_kind is FailureKind.MODEL_OUTPUT_INVALID
    assert outcome.repair_attempts_used == answer_module.MAX_REPAIR_ATTEMPTS


# ---------------------------------------------------------------------------
# The bound itself: repair never exceeds MAX_REPAIR_ATTEMPTS regardless of
# which failure kind keeps recurring.
# ---------------------------------------------------------------------------

@requires_db
def test_repair_loop_is_bounded_not_unbounded(pg_conn, monkeypatch):
    from app.pipeline.generate import build_context

    ctx = build_context(pg_conn)
    calls = _stub_sequence(monkeypatch, _gen_result(sql="SELECT id FROM analytics.v_typo_forever"))

    outcome = answer_module.answer(ctx, "anything", tenant_id=1)
    assert outcome.verdict is Action.GIVE_UP
    assert calls["n"] == answer_module.MAX_REPAIR_ATTEMPTS + 1  # initial + MAX repairs, never more
