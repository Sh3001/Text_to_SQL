"""HTTP surface. Two SSE endpoints implement the design doc's approval
gate: POST /api/query runs generate -> guard -> budget (plan.plan()) and
either auto-executes when the model's own confidence is "high", or
pauses and streams an `awaiting_approval` event carrying the SQL for a
human to read and approve — never silently executing a medium/low
-confidence guess. POST /api/query/approve resumes a paused plan,
optionally with human-edited SQL, which is re-guarded exactly like model
output before it runs (editing SQL is not a privileged path).

Pending plans live in an in-memory dict (app.state.pending_plans),
keyed by a UUID — not the `query_runs` Postgres table the design doc
eventually wants. A real deployment needs that table so a plan survives
a server restart; this is a real, named gap, not an oversight (see
README's Phase 06 section).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

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


@router.get("/api/health")
async def health(request: Request) -> dict:
    ctx = request.app.state.ctx
    return {"status": "ok", "schema_fingerprint": ctx.snapshot.fingerprint}


@router.post("/api/query")
async def query(req: QueryRequest, request: Request) -> StreamingResponse:
    ctx = request.app.state.ctx

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
            return

        if plan_result.plan.confidence == "high":
            # High confidence — run immediately, same stream, no approval
            # round trip. Medium/low always pause (see approval gate note
            # above); a future cost-based trigger ("over the plan-cost
            # threshold" even at high confidence) is deferred — see README.
            def do_finish(on_event):
                return answer_module.finish(plan_result, tenant_id=req.tenant_id, on_event=on_event, catalog=ctx.catalog)

            async for kind, subkind, payload in run_with_events(do_finish):
                if kind == "progress":
                    yield sse_format(subkind, payload)
                elif kind == "error":
                    yield sse_format("error", payload)
                elif kind == "result":
                    yield sse_format("done", serialize_outcome(payload))
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

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/api/query/{plan_id}/reject")
async def reject(plan_id: str, request: Request) -> dict:
    request.app.state.pending_plans.pop(plan_id, None)
    return {"status": "discarded"}
