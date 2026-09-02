"""The bounded repair loop — Phase 04's centerpiece. Ties generate, guard,
plan_budget, execute, and diagnose together into the actual error taxonomy
from the project plan: each failure gets classified once (errors.py) and
handled by exactly the action its classification says, never by ad hoc
handling scattered through the loop.

Two attempts, tracked and enforced here, not left to hope: a missing
column or an over-budget query gets the real error fed back and one more
try; a security violation never gets a second try at all; and when the
budget is spent, the loop stops and says so plainly rather than looping
forever against a confused model — see MAX_REPAIR_ATTEMPTS and the
plan's "bound the loop and mean it" callout.
"""

from __future__ import annotations

from dataclasses import dataclass

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


def answer(
    ctx: PipelineContext,
    question: str,
    tenant_id: int = 1,
    model: str | None = None,
) -> AnswerOutcome:
    repair_attempts: list[RepairAttempt] = []
    gen_kwargs = {"model": model} if model else {}

    for attempt_num in range(MAX_REPAIR_ATTEMPTS + 1):
        try:
            gen = generate_plan(ctx, question, repair_attempts=repair_attempts, **gen_kwargs)
        except OutputParseError as exc:
            if attempt_num < MAX_REPAIR_ATTEMPTS:
                repair_attempts.append(RepairAttempt(sql="(no SQL — output didn't parse)", error=str(exc)))
                continue
            return AnswerOutcome(
                Action.GIVE_UP, question, None, None, None,
                f"model output never validated after {MAX_REPAIR_ATTEMPTS} repair attempts: {exc}",
                attempt_num, None, FailureKind.MODEL_OUTPUT_INVALID,
            )
        except ModelUnavailableError as exc:
            return AnswerOutcome(
                Action.GIVE_UP, question, None, None, None, str(exc),
                attempt_num, None, FailureKind.MODEL_UNAVAILABLE,
            )
        except GenerationError as exc:  # ModelResponseError and any other infra failure
            return AnswerOutcome(
                Action.GIVE_UP, question, None, None, None, str(exc),
                attempt_num, None, FailureKind.MODEL_UNAVAILABLE,
            )

        plan = gen.plan
        if plan.needs_clarification:
            return AnswerOutcome(
                Action.ASK, question, plan, None, None, plan.clarifying_question or "",
                attempt_num, plan.sql, FailureKind.AMBIGUOUS,
            )

        guard_result = ast_guard.check(plan.sql, catalog=ctx.catalog)
        if not guard_result.ok:
            kind, action = classify_guard_rejection(guard_result.reason, guard_result.terminal)
            if action is Action.BLOCK:
                return AnswerOutcome(
                    Action.BLOCK, question, plan, None, None, guard_result.detail,
                    attempt_num, plan.sql, kind,
                )
            if attempt_num < MAX_REPAIR_ATTEMPTS:
                repair_attempts.append(
                    RepairAttempt(sql=plan.sql, error=f"{guard_result.reason.value}: {guard_result.detail}")
                )
                continue
            return AnswerOutcome(
                Action.GIVE_UP, question, plan, None, None,
                f"still invalid after {MAX_REPAIR_ATTEMPTS} repair attempts: {guard_result.detail}",
                attempt_num, plan.sql, kind,
            )

        try:
            budget_result = plan_budget.check(guard_result.safe_sql, tenant_id)
        except ExecutionError as exc:
            # EXPLAIN itself can fail with a real Postgres error — Postgres
            # constant-folds immutable expressions during planning, so
            # `SELECT 1/0` raises division-by-zero under EXPLAIN even
            # though it never actually executes. Route it through the same
            # classification as an execution failure rather than letting
            # it escape uncaught (found by testing: an early version of
            # this loop only wrapped execute() in try/except, not the
            # budget check, and this exact query crashed the whole loop).
            kind, action = classify_execution_error(exc)
            if action is Action.ASK:
                return AnswerOutcome(
                    Action.ASK, question, plan, None, None,
                    f"query timed out while estimating its cost — try a narrower filter ({exc})",
                    attempt_num, guard_result.safe_sql, kind,
                )
            if attempt_num < MAX_REPAIR_ATTEMPTS:
                repair_attempts.append(RepairAttempt(sql=guard_result.safe_sql, error=str(exc)))
                continue
            return AnswerOutcome(
                Action.GIVE_UP, question, plan, None, None,
                f"query failed during planning after {MAX_REPAIR_ATTEMPTS} repair attempts: {exc}",
                attempt_num, guard_result.safe_sql, kind,
            )

        if not budget_result.ok:
            if attempt_num < MAX_REPAIR_ATTEMPTS:
                repair_attempts.append(
                    RepairAttempt(sql=guard_result.safe_sql, error=f"query over plan budget: {budget_result.detail}")
                )
                continue
            return AnswerOutcome(
                Action.GIVE_UP, question, plan, None, None,
                f"query too expensive after {MAX_REPAIR_ATTEMPTS} repair attempts: {budget_result.detail}",
                attempt_num, guard_result.safe_sql, FailureKind.OVER_BUDGET,
            )

        try:
            exec_result = execute(guard_result.safe_sql, tenant_id=tenant_id)
        except ExecutionError as exc:
            kind, action = classify_execution_error(exc)
            if action is Action.ASK:  # timeout — never silently retried, see the plan's error table
                return AnswerOutcome(
                    Action.ASK, question, plan, None, None,
                    f"query timed out — try a narrower date range or an extra filter ({exc})",
                    attempt_num, guard_result.safe_sql, kind,
                )
            if attempt_num < MAX_REPAIR_ATTEMPTS:
                repair_attempts.append(RepairAttempt(sql=guard_result.safe_sql, error=str(exc)))
                continue
            return AnswerOutcome(
                Action.GIVE_UP, question, plan, None, None,
                f"execution failed after {MAX_REPAIR_ATTEMPTS} repair attempts: {exc}",
                attempt_num, guard_result.safe_sql, kind,
            )

        if exec_result.row_count == 0:
            diagnosis = diagnose_zero_rows(guard_result.safe_sql, tenant_id, catalog=ctx.catalog)
            message = diagnosis.message if diagnosis is not None else "the query returned no rows"
            return AnswerOutcome(
                Action.DIAGNOSE, question, plan, exec_result, diagnosis, message,
                attempt_num, guard_result.safe_sql, FailureKind.ZERO_ROWS,
            )

        return AnswerOutcome(
            Action.ANSWERED, question, plan, exec_result, None, "answered",
            attempt_num, guard_result.safe_sql, None,
        )

    raise AssertionError("repair loop exited its range without returning — MAX_REPAIR_ATTEMPTS logic bug")
