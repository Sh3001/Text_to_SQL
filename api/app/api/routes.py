"""HTTP surface. POST /api/query runs generate -> guard -> budget and
either auto-executes at "high" confidence, or pauses and streams an
`awaiting_approval` event for a human to read and approve. POST
/api/query/approve resumes a paused plan, optionally with human-edited
SQL, re-guarded exactly like model output.

Pending plans live in an in-memory dict (app.state.pending_plans), not
Postgres — a known gap, see README; terminal outcomes ARE persisted to
audit.query_log after each SSE stream finishes. GET /api/stats and
GET /api/audit read that table back.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..obs.audit import log_query_run
from ..obs.stats import get_recent_events, get_summary
from ..pipeline import answer as answer_module
from .sse import run_with_events, serialize_outcome, serialize_plan_result, sse_format

router = APIRouter()


class QueryRequest(BaseModel):
    question: str
    tenant_id: int = 1
    model: str | None = None


class ApproveRequest(BaseModel):
    plan_id: str
    sql: str | None = None  # human-edited SQL, if the query card's textarea was changed
    tenant_id: int = 1


def _log_safely(outcome, tenant_id: int, model: str, edited: bool = False) -> None:
    # A logging failure must never surface as a failure of the already
    # -streamed request — swallowed here, deliberately.
    try:
        log_query_run(outcome, tenant_id=tenant_id, model=model, edited=edited)
    except Exception as exc:  # noqa: BLE001
        print(f"[audit] failed to log query run: {exc}")


@router.get("/api/health")
async def health(request: Request) -> dict:
    ctx = request.app.state.ctx
    return {"status": "ok", "schema_fingerprint": ctx.snapshot.fingerprint}


@router.get("/api/stats")
async def stats(hours: int = 24) -> dict:
    return get_summary(hours=hours)


@router.get("/api/audit")
async def audit(limit: int = 50) -> dict:
    return {"events": get_recent_events(limit=limit)}


@router.post("/api/query")
async def query(req: QueryRequest, request: Request) -> StreamingResponse:
    ctx = request.app.state.ctx
    model_name = req.model or _default_model_name()

    async def gen():
        def do_plan(on_event):
            return answer_module.plan(
                ctx, req.question, tenant_id=req.tenant_id, model=req.model, on_event=on_event,
            )

        plan_result = None
        async for kind, subkind, payload in run_with_events(do_plan):
            if kind == "progress":
                yield sse_format(subkind, payload)
            elif kind == "error":
                yield sse_format("error", payload)
                return
            elif kind == "result":
                plan_result = payload

        if plan_result is None:
            return

        if not plan_result.ready:
            yield sse_format("done", serialize_outcome(plan_result.terminal_outcome))
            _log_safely(plan_result.terminal_outcome, req.tenant_id, model_name)
            return

        if plan_result.plan.confidence == "high":
            # Medium/low always pause. A future cost-based trigger even
            # at high confidence is deferred — see README.
            def do_finish(on_event):
                return answer_module.finish(plan_result, tenant_id=req.tenant_id, on_event=on_event, catalog=ctx.catalog)

            async for kind, subkind, payload in run_with_events(do_finish):
                if kind == "progress":
                    yield sse_format(subkind, payload)
                elif kind == "error":
                    yield sse_format("error", payload)
                elif kind == "result":
                    yield sse_format("done", serialize_outcome(payload))
                    _log_safely(payload, req.tenant_id, model_name)
            return

        plan_id = str(uuid.uuid4())
        request.app.state.pending_plans[plan_id] = plan_result
        yield sse_format("awaiting_approval", {"plan_id": plan_id, **serialize_plan_result(plan_result)})

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/api/query/approve")
async def approve(req: ApproveRequest, request: Request) -> StreamingResponse:
    ctx = request.app.state.ctx
    plan_result = request.app.state.pending_plans.pop(req.plan_id, None)
    if plan_result is None:
        raise HTTPException(status_code=404, detail="unknown or already-resolved plan_id")

    edited = req.sql is not None and req.sql.strip() != (plan_result.safe_sql or "").strip()
    model_name = _default_model_name()

    async def gen():
        def do_finish(on_event):
            return answer_module.finish(
                plan_result, tenant_id=req.tenant_id, on_event=on_event,
                override_sql=req.sql, catalog=ctx.catalog,
            )

        async for kind, subkind, payload in run_with_events(do_finish):
            if kind == "progress":
                yield sse_format(subkind, payload)
            elif kind == "error":
                yield sse_format("error", payload)
            elif kind == "result":
                yield sse_format("done", serialize_outcome(payload))
                _log_safely(payload, req.tenant_id, model_name, edited=edited)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/api/query/{plan_id}/reject")
async def reject(plan_id: str, request: Request) -> dict:
    # Not logged to audit.query_log — a discarded plan is lower-stakes
    # than the table's five real verdicts (see README).
    request.app.state.pending_plans.pop(plan_id, None)
    return {"status": "discarded"}


def _default_model_name() -> str:
    from ..llm.client import DEFAULT_MODEL

    return DEFAULT_MODEL
