"""CLI driver for the generate -> guard -> budget -> execute -> diagnose
loop (pipeline/answer.py). Prints enough of the outcome to see which
action fired and why — not the eval harness, which grades against
golden answers.
"""

from __future__ import annotations

import argparse
import os

import psycopg

from .answer import AnswerOutcome, answer
from .errors import Action
from .generate import build_context

DEFAULT_DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/querywarden"
)


def _print_outcome(o: AnswerOutcome) -> None:
    print(f"\n{'=' * 78}")
    print(f"Q: {o.question}")
    print(f"{'-' * 78}")

    if o.plan is not None:
        print(f"intent:       {o.plan.intent}")
        if o.plan.assumptions:
            print(f"assumptions:  {'; '.join(o.plan.assumptions)}")
        print(f"confidence:   {o.plan.confidence}")

    print(f"verdict:      {o.verdict.value}"
          + (f"  ({o.failure_kind.value})" if o.failure_kind else "")
          + (f"  [{o.repair_attempts_used} repair attempt(s)]" if o.repair_attempts_used else ""))

    if o.verdict is Action.ANSWERED:
        print(f"sql:          {o.last_sql}")
        print(f"rows:         {o.execution.row_count}  ({o.execution.duration_ms:.0f}ms)")
        for row in o.execution.rows[:5]:
            print(f"  {row}")
        if o.execution.row_count > 5:
            print(f"  ... ({o.execution.row_count - 5} more)")
    elif o.verdict is Action.ASK:
        print(f"ASK:          {o.message}")
    elif o.verdict is Action.BLOCK:
        print(f"sql:          {o.last_sql}")
        print(f"BLOCKED:      {o.message}")
    elif o.verdict is Action.DIAGNOSE:
        print(f"sql:          {o.last_sql}")
        print(f"ZERO ROWS:    {o.message}")
        if o.diagnosis:
            for check in o.diagnosis.checks:
                marker = " <-- likely culprit" if check.predicate_sql == o.diagnosis.culprit else ""
                print(f"  {check.predicate_sql:50} -> {check.cumulative_row_count} rows{marker}")
    elif o.verdict is Action.GIVE_UP:
        print(f"sql:          {o.last_sql}")
        print(f"GAVE UP:      {o.message}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Query Warden — generate/guard/budget/execute/diagnose CLI")
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
        counts: dict[str, int] = {}
        for o in outcomes:
            counts[o.verdict.value] = counts.get(o.verdict.value, 0) + 1
        summary = ", ".join(f"{n} {v}" for v, n in sorted(counts.items()))
        print(f"\n{'=' * 78}\n{summary} — of {len(outcomes)} total")


if __name__ == "__main__":
    main()
