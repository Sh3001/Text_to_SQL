"""The Phase 03 checkpoint driver: generate -> guard -> execute, for one
question or a file of them, printed for human inspection. Not the eval
harness (Phase 05, which grades against golden answers) — this is the
"twenty hand-written questions answered end to end" checkpoint from the
project plan, meant to be read, not asserted on.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass

import psycopg

from ..guards import ast_guard
from ..guards.errors import RejectReason
from ..llm.client import GenerationError, GenerationResult
from ..llm.schemas import SqlPlan
from .execute import ExecutionError, ExecutionResult, execute
from .generate import PipelineContext, build_context, generate_plan

DEFAULT_DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/querywarden"
)


@dataclass
class Outcome:
    question: str
    generation: GenerationResult | None
    guard: ast_guard.GuardResult | None
    execution: ExecutionResult | None
    error: str | None
    wall_ms: float


def answer(ctx: PipelineContext, question: str, tenant_id: int = 1, model: str | None = None) -> Outcome:
    t0 = time.monotonic()
    try:
        gen = generate_plan(ctx, question, model=model) if model else generate_plan(ctx, question)
    except GenerationError as exc:
        return Outcome(question, None, None, None, str(exc), (time.monotonic() - t0) * 1000)

    plan = gen.plan
    if plan.needs_clarification:
        return Outcome(question, gen, None, None, None, (time.monotonic() - t0) * 1000)

    guard_result = ast_guard.check(plan.sql, catalog=ctx.catalog)
    if not guard_result.ok:
        return Outcome(question, gen, guard_result, None, None, (time.monotonic() - t0) * 1000)

    try:
        exec_result = execute(guard_result.safe_sql, tenant_id=tenant_id)
    except ExecutionError as exc:
        return Outcome(question, gen, guard_result, None, str(exc), (time.monotonic() - t0) * 1000)

    return Outcome(question, gen, guard_result, exec_result, None, (time.monotonic() - t0) * 1000)


def _print_outcome(o: Outcome) -> None:
    print(f"\n{'=' * 78}")
    print(f"Q: {o.question}")
    print(f"{'-' * 78}")
    if o.generation is None:
        print(f"GENERATION FAILED: {o.error}")
        return

    plan: SqlPlan = o.generation.plan
    print(f"intent:       {plan.intent}")
    if plan.assumptions:
        print(f"assumptions:  {'; '.join(plan.assumptions)}")
    print(f"confidence:   {plan.confidence}")

    if plan.needs_clarification:
        print(f"CLARIFY:      {plan.clarifying_question}")
        return

    print(f"tables_used:  {', '.join(plan.tables_used) or '(none)'}")
    print(f"sql (model):  {plan.sql}")

    if o.guard is None:
        return
    if not o.guard.ok:
        marker = "TERMINAL" if o.guard.terminal else "repairable"
        print(f"GUARD REJECTED [{marker}]: {o.guard.reason} — {o.guard.detail}")
        return

    print(f"sql (safe):   {o.guard.safe_sql}")

    if o.error:
        print(f"EXECUTION FAILED: {o.error}")
        return
    if o.execution is not None:
        print(f"rows:         {o.execution.row_count}  ({o.execution.duration_ms:.0f}ms)")
        for row in o.execution.rows[:5]:
            print(f"  {row}")
        if o.execution.row_count > 5:
            print(f"  ... ({o.execution.row_count - 5} more)")
    print(f"wall time:    {o.wall_ms:.0f}ms  "
          f"(model: {o.generation.prompt_eval_count}p+{o.generation.eval_count}e tokens)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Query Warden — generate/guard/execute CLI")
    parser.add_argument("question", nargs="?", help="A single question. Omit to use --file.")
    parser.add_argument("--file", help="Path to a file of newline-separated questions.")
    parser.add_argument("--tenant-id", type=int, default=1)
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    if not args.question and not args.file:
        parser.error("provide a question or --file")

    with psycopg.connect(DEFAULT_DATABASE_URL) as conn:
        ctx = build_context(conn)

    questions = [args.question] if args.question else [
        line.strip() for line in open(args.file, encoding="utf-8") if line.strip() and not line.startswith("#")
    ]

    outcomes = []
    for q in questions:
        o = answer(ctx, q, tenant_id=args.tenant_id, model=args.model)
        _print_outcome(o)
        outcomes.append(o)

    if len(outcomes) > 1:
        ok = sum(1 for o in outcomes if o.execution is not None)
        clarified = sum(1 for o in outcomes if o.generation and o.generation.plan.needs_clarification)
        blocked = sum(1 for o in outcomes if o.guard is not None and not o.guard.ok)
        failed = sum(1 for o in outcomes if o.error is not None)
        print(f"\n{'=' * 78}\n{ok} executed, {clarified} clarified, {blocked} guard-blocked, "
              f"{failed} failed — of {len(outcomes)} total")


if __name__ == "__main__":
    main()
