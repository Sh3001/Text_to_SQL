"""Bridges synchronous, callback-instrumented pipeline calls (answer.plan
/ answer.finish — both blocking, they hit Ollama and Postgres over the
network) into async SSE streams, without adding a dependency.

`run_with_events` is the one primitive: it runs `work(on_event)` in a
worker thread, where `work` calls `on_event(kind, payload)` as many times
as it wants (real progress events) and returns a final result. The async
generator yields `("progress", kind, payload)` for each on_event call and
finally `("result", None, work's return value)` — or `("error", None,
{"message": ...})` if `work` raised. Two different endpoint shapes
(POST /api/query auto-executes or pauses for approval depending on
confidence; POST /api/query/approve always executes) both build on this
same primitive rather than forcing one generic wrapper to cover both —
see routes.py for how each uses it.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from collections.abc import AsyncGenerator, Callable
from typing import Any

from ..pipeline.answer import AnswerOutcome, EventCallback

_SENTINEL = object()


def sse_format(event: str, data: dict) -> str:
    # SSE wire format: "event: <name>\ndata: <json>\n\n" — the blank line
    # is what terminates one event. default=str is the fallback for raw
    # DB row values json.dumps doesn't natively know (Decimal, datetime)
    # — left unconverted upstream so a numeric-looking Decimal reads as
    # the exact string the database produced, not a re-derived float.
    payload = json.dumps(data, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


async def run_with_events(work: Callable[[EventCallback], Any]) -> AsyncGenerator[tuple[str, str | None, Any], None]:
    q: queue.Queue = queue.Queue()
    loop = asyncio.get_event_loop()

    def on_event(kind: str, payload: dict) -> None:
        q.put(("progress", kind, payload))

    def worker() -> None:
        try:
            result = work(on_event)
            q.put(("result", None, result))
        except Exception as exc:  # surfaced as an error event, never silently dropped
            q.put(("error", None, {"message": str(exc)}))
        finally:
            q.put(_SENTINEL)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    while True:
        item = await loop.run_in_executor(None, q.get)
        if item is _SENTINEL:
            break
        yield item


def serialize_outcome(outcome: AnswerOutcome) -> dict:
    """Explicit, not reflection-based — the object graph (AnswerOutcome ->
    SqlPlan / ExecutionResult / ZeroRowDiagnosis) is small and fixed, and
    writing out exactly what each field means here is worth more than a
    generic recursive serializer that would silently do the wrong thing
    the day one of these dataclasses gains a field.
    """
    return {
        "verdict": outcome.verdict.value,
        "question": outcome.question,
        "plan": _serialize_plan(outcome.plan),
        "execution": _serialize_execution(outcome.execution),
        "diagnosis": _serialize_diagnosis(outcome.diagnosis),
        "message": outcome.message,
        "repair_attempts_used": outcome.repair_attempts_used,
        "last_sql": outcome.last_sql,
        "failure_kind": outcome.failure_kind.value if outcome.failure_kind else None,
    }


def serialize_plan_result(result) -> dict:
    return {
        "ready": result.ready,
        "question": result.question,
        "plan": _serialize_plan(result.plan),
        "safe_sql": result.safe_sql,
        "repair_attempts_used": result.repair_attempts_used,
        "terminal_outcome": serialize_outcome(result.terminal_outcome) if result.terminal_outcome else None,
    }


def _serialize_plan(plan) -> dict | None:
    return None if plan is None else plan.model_dump()


def _serialize_execution(execution) -> dict | None:
    if execution is None:
        return None
    return {
        "columns": execution.columns,
        "rows": execution.rows,  # raw DB values (Decimal/datetime included) — see sse_format's default=str
        "row_count": execution.row_count,
        "duration_ms": execution.duration_ms,
    }


def _serialize_diagnosis(diagnosis) -> dict | None:
    if diagnosis is None:
        return None
    return {
        "baseline_row_count": diagnosis.baseline_row_count,
        "checks": [
            {"predicate_sql": c.predicate_sql, "cumulative_row_count": c.cumulative_row_count}
            for c in diagnosis.checks
        ],
        "culprit": diagnosis.culprit,
        "message": diagnosis.message,
    }
