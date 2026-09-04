"""The bounded repair loop. Ties generate, guard, plan_budget, execute,
and diagnose together into the error taxonomy (errors.py): each failure
is classified once and handled by exactly the action its class calls
for. Two attempts, enforced via MAX_REPAIR_ATTEMPTS — a missing column
gets the real error fed back and one more try; a security violation
never gets a second try; when the budget is spent, the loop stops and
says so.

Split in two for the API's approval gate: `plan()` runs generate ->
guard -> budget with the repair loop and stops right before execution;
`finish()` runs execute() + the zero-row diagnostic. `answer()` composes
the two. The optional `on_event` callback fires at each real state
transition so the API can stream genuine progress.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..guards import ast_guard
from ..llm.client import GenerationError, ModelUnavailableError, OutputParseError
from ..llm.prompts import RepairAttempt
from ..llm.schemas import SqlPlan
from . import plan_budget
from .diagnose import ZeroRowDiagnosis, diagnose_zero_rows
from .errors import Action, FailureKind, classify_execution_error, classify_guard_rejection
from .execute import ExecutionError, ExecutionResult, execute
from .generate import PipelineContext, generate_plan

MAX_REPAIR_ATTEMPTS = 2

EventCallback = Callable[[str, dict], None]


def _emit(on_event: EventCallback | None, kind: str, **payload) -> None:
    if on_event is not None:
        on_event(kind, payload)


@dataclass
class AnswerOutcome:
    verdict: Action
    question: str
    plan: SqlPlan | None
    execution: ExecutionResult | None
    diagnosis: ZeroRowDiagnosis | None
    message: str
    repair_attempts_used: int
    last_sql: str | None
    failure_kind: FailureKind | None


@dataclass
class PlanResult:
    """Output of plan(): either a ready-to-execute (plan, safe_sql) pair,
    or a terminal outcome reached before execution (ambiguous, blocked,
    or repair budget exhausted)."""
    ready: bool
    question: str
    plan: SqlPlan | None
    safe_sql: str | None
    repair_attempts_used: int
    terminal_outcome: AnswerOutcome | None = None


def plan(
    ctx: PipelineContext,
    question: str,
    tenant_id: int = 1,
    model: str | None = None,
    on_event: EventCallback | None = None,
) -> PlanResult:
    repair_attempts: list[RepairAttempt] = []
    gen_kwargs = {"model": model} if model else {}

    for attempt_num in range(MAX_REPAIR_ATTEMPTS + 1):
        _emit(on_event, "generating", attempt=attempt_num)
        try:
            gen = generate_plan(ctx, question, repair_attempts=repair_attempts, **gen_kwargs)
        except OutputParseError as exc:
            if attempt_num < MAX_REPAIR_ATTEMPTS:
                repair_attempts.append(RepairAttempt(sql="(no SQL — output didn't parse)", error=str(exc)))
                _emit(on_event, "repairing", attempt=attempt_num, reason=str(exc))
                continue
            return PlanResult(False, question, None, None, attempt_num, AnswerOutcome(
                Action.GIVE_UP, question, None, None, None,
                f"model output never validated after {MAX_REPAIR_ATTEMPTS} repair attempts: {exc}",
                attempt_num, None, FailureKind.MODEL_OUTPUT_INVALID,
            ))
        except ModelUnavailableError as exc:
            return PlanResult(False, question, None, None, attempt_num, AnswerOutcome(
                Action.GIVE_UP, question, None, None, None, str(exc),
                attempt_num, None, FailureKind.MODEL_UNAVAILABLE,
            ))
        except GenerationError as exc:  # ModelResponseError and any other infra failure
            return PlanResult(False, question, None, None, attempt_num, AnswerOutcome(
                Action.GIVE_UP, question, None, None, None, str(exc),
                attempt_num, None, FailureKind.MODEL_UNAVAILABLE,
            ))

        gen_plan = gen.plan
        _emit(
            on_event, "plan", attempt=attempt_num, intent=gen_plan.intent,
            assumptions=gen_plan.assumptions, sql=gen_plan.sql, confidence=gen_plan.confidence,
            tables_used=gen_plan.tables_used,
            chart=gen_plan.chart.model_dump() if gen_plan.chart else None,
            clarifying_question=gen_plan.clarifying_question,
        )

        if gen_plan.needs_clarification:
            return PlanResult(False, question, gen_plan, None, attempt_num, AnswerOutcome(
                Action.ASK, question, gen_plan, None, None, gen_plan.clarifying_question or "",
                attempt_num, gen_plan.sql, FailureKind.AMBIGUOUS,
            ))

        guard_result = ast_guard.check(gen_plan.sql, catalog=ctx.catalog)
        _emit(
            on_event, "guard_result", attempt=attempt_num, ok=guard_result.ok,
            reason=guard_result.reason.value if guard_result.reason else None, detail=guard_result.detail,
        )
        if not guard_result.ok:
            kind, action = classify_guard_rejection(guard_result.reason, guard_result.terminal)
            if action is Action.BLOCK:
                return PlanResult(False, question, gen_plan, None, attempt_num, AnswerOutcome(
                    Action.BLOCK, question, gen_plan, None, None, guard_result.detail,
                    attempt_num, gen_plan.sql, kind,
                ))
            if attempt_num < MAX_REPAIR_ATTEMPTS:
                repair_attempts.append(
                    RepairAttempt(sql=gen_plan.sql, error=f"{guard_result.reason.value}: {guard_result.detail}")
                )
                _emit(on_event, "repairing", attempt=attempt_num, reason=guard_result.detail)
                continue
            return PlanResult(False, question, gen_plan, None, attempt_num, AnswerOutcome(
                Action.GIVE_UP, question, gen_plan, None, None,
                f"still invalid after {MAX_REPAIR_ATTEMPTS} repair attempts: {guard_result.detail}",
                attempt_num, gen_plan.sql, kind,
            ))

        try:
            budget_result = plan_budget.check(guard_result.safe_sql, tenant_id)
        except ExecutionError as exc:
            # EXPLAIN itself can fail with a real Postgres error (constant
            # folding — `SELECT 1/0` raises under EXPLAIN even though it
            # never executes); classify it the same as an execution failure.
            kind, action = classify_execution_error(exc)
            if action is Action.ASK:
                return PlanResult(False, question, gen_plan, None, attempt_num, AnswerOutcome(
                    Action.ASK, question, gen_plan, None, None,
                    f"query timed out while estimating its cost — try a narrower filter ({exc})",
                    attempt_num, guard_result.safe_sql, kind,
                ))
            if attempt_num < MAX_REPAIR_ATTEMPTS:
                repair_attempts.append(RepairAttempt(sql=guard_result.safe_sql, error=str(exc)))
                _emit(on_event, "repairing", attempt=attempt_num, reason=str(exc))
                continue
            return PlanResult(False, question, gen_plan, None, attempt_num, AnswerOutcome(
                Action.GIVE_UP, question, gen_plan, None, None,
                f"query failed during planning after {MAX_REPAIR_ATTEMPTS} repair attempts: {exc}",
                attempt_num, guard_result.safe_sql, kind,
            ))

        _emit(
            on_event, "budget_result", attempt=attempt_num, ok=budget_result.ok,
            estimated_cost=budget_result.estimated_cost, estimated_rows=budget_result.estimated_rows,
        )
        if not budget_result.ok:
            if attempt_num < MAX_REPAIR_ATTEMPTS:
                repair_attempts.append(
                    RepairAttempt(sql=guard_result.safe_sql, error=f"query over plan budget: {budget_result.detail}")
                )
                _emit(on_event, "repairing", attempt=attempt_num, reason=budget_result.detail)
                continue
            return PlanResult(False, question, gen_plan, None, attempt_num, AnswerOutcome(
                Action.GIVE_UP, question, gen_plan, None, None,
                f"query too expensive after {MAX_REPAIR_ATTEMPTS} repair attempts: {budget_result.detail}",
                attempt_num, guard_result.safe_sql, FailureKind.OVER_BUDGET,
            ))

        return PlanResult(True, question, gen_plan, guard_result.safe_sql, attempt_num)

    raise AssertionError("repair loop exited its range without returning — MAX_REPAIR_ATTEMPTS logic bug")


def finish(
    result: PlanResult,
    tenant_id: int = 1,
    on_event: EventCallback | None = None,
    override_sql: str | None = None,
    catalog=None,
) -> AnswerOutcome:
    """Runs execute() + the zero-row diagnostic for a ready PlanResult.
    A non-ready result's terminal_outcome is returned as-is, so
    `finish(plan(...))` is always safe to call uniformly.

    `override_sql` lets an approval-gated caller substitute human-edited
    SQL — re-guarded here, never trusted, exactly like model output.
    `catalog` is required only when `override_sql` is given.
    """
    if not result.ready:
        return result.terminal_outcome

    safe_sql = result.safe_sql
    if override_sql is not None and override_sql.strip() != (result.safe_sql or "").strip():
        guard_result = ast_guard.check(override_sql, catalog=catalog)
        _emit(
            on_event, "guard_result", attempt=result.repair_attempts_used, ok=guard_result.ok,
            reason=guard_result.reason.value if guard_result.reason else None, detail=guard_result.detail,
        )
        if not guard_result.ok:
            kind, action = classify_guard_rejection(guard_result.reason, guard_result.terminal)
            verdict = Action.BLOCK if action is Action.BLOCK else Action.GIVE_UP
            return AnswerOutcome(
                verdict, result.question, result.plan, None, None, guard_result.detail,
                result.repair_attempts_used, override_sql, kind,
            )
        safe_sql = guard_result.safe_sql

    _emit(on_event, "executing", sql=safe_sql)
    try:
        exec_result = execute(safe_sql, tenant_id=tenant_id)
    except ExecutionError as exc:
        kind, action = classify_execution_error(exc)
        message = (
            f"query timed out — try a narrower date range or an extra filter ({exc})"
            if action is Action.ASK else f"execution failed: {exc}"
        )
        verdict = Action.ASK if action is Action.ASK else Action.GIVE_UP
        _emit(on_event, "error", message=message)
        return AnswerOutcome(
            verdict, result.question, result.plan, None, None, message,
            result.repair_attempts_used, safe_sql, kind,
        )

    if exec_result.row_count == 0:
        diagnosis = diagnose_zero_rows(safe_sql, tenant_id, catalog=catalog)
        message = diagnosis.message if diagnosis is not None else "the query returned no rows"
        _emit(on_event, "diagnosis", message=message)
        return AnswerOutcome(
            Action.DIAGNOSE, result.question, result.plan, exec_result, diagnosis, message,
            result.repair_attempts_used, safe_sql, FailureKind.ZERO_ROWS,
        )

    _emit(
        on_event, "result", columns=exec_result.columns,
        row_count=exec_result.row_count, duration_ms=exec_result.duration_ms,
    )
    return AnswerOutcome(
        Action.ANSWERED, result.question, result.plan, exec_result, None, "answered",
        result.repair_attempts_used, safe_sql, None,
    )


def answer(
    ctx: PipelineContext,
    question: str,
    tenant_id: int = 1,
    model: str | None = None,
    on_event: EventCallback | None = None,
) -> AnswerOutcome:
    result = plan(ctx, question, tenant_id=tenant_id, model=model, on_event=on_event)
    return finish(result, tenant_id=tenant_id, on_event=on_event, catalog=ctx.catalog)
