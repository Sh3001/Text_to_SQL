"""Bridges synchronous, callback-instrumented pipeline calls (both
blocking — they hit Ollama and Postgres) into async SSE streams, without
adding a dependency.

`run_with_events` is the one primitive: runs `work(on_event)` in a
worker thread and yields `("progress", kind, payload)` for each
on_event call, then `("result", None, return value)` or `("error",
None, {"message": ...})` if `work` raised.
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
    # default=str handles raw DB values (Decimal, datetime) json.dumps
    # doesn't natively know.
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
    """Explicit, not reflection-based — the object graph is small and
    fixed, and a generic recursive serializer would silently do the
    wrong thing the day a dataclass gains a field."""
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
        "rows": execution.rows,  # raw DB values — see sse_format's default=str
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
