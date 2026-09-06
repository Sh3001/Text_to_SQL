"""HTTP surface. POST /api/query runs generate -> guard -> budget and
either auto-executes at "high" confidence, or pauses and streams an
`awaiting_approval` event for a human to read and approve. POST
/api/query/approve resumes a paused plan, optionally with human-edited
SQL, re-guarded exactly like model output.

These routes are open — there is no authentication. The tenant a query
runs under still does NOT come from the request: it is read from server
configuration (app/tenant.py). Unauthenticated and caller-controlled are
different things, and a `tenant_id` in the body was a real hole once.

Pending plans live in an in-memory dict (app.state.pending_plans), not
Postgres — a known gap, see README. With no accounts, anyone who has a
plan_id can approve it.

Terminal outcomes are persisted to audit.query_log, and to the given
conversation's history when one is supplied.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..history import store as history_store
from ..obs.audit import log_query_run
from ..obs.stats import get_recent_events, get_summary
from ..pipeline import answer as answer_module
from ..tenant import current_tenant_id
from .sse import run_with_events, serialize_outcome, serialize_plan_result, sse_format

router = APIRouter()


class QueryRequest(BaseModel):
    question: str
    model: str | None = None
    conversation_id: str | None = None


class ApproveRequest(BaseModel):
    plan_id: str
    sql: str | None = None  # human-edited SQL, if the query card's textarea was changed


@dataclass
class PendingPlan:
    """A plan parked between /query and /query/approve."""
    plan_result: object
    tenant_id: int
    question: str
    conversation_id: str | None


def _log_safely(outcome, tenant_id: int, model: str, edited: bool = False) -> None:
    # A logging failure must never surface as a failure of the already
    # -streamed request — swallowed here, deliberately.
    try:
        log_query_run(outcome, tenant_id=tenant_id, model=model, edited=edited)
    except Exception as exc:  # noqa: BLE001
        print(f"[audit] failed to log query run: {exc}")


def _save_turn_safely(conversation_id, tenant_id, question, outcome_payload) -> None:
    """History is a convenience, not the answer. A write failure here is
    logged and swallowed, same reasoning as the audit write."""
    if not conversation_id:
        return
    try:
        history_store.append_turn(conversation_id, tenant_id, question, outcome_payload)
    except Exception as exc:  # noqa: BLE001
        print(f"[history] failed to save turn: {exc}")


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
    tenant_id = current_tenant_id()

    async def gen():
        def do_plan(on_event):
            return answer_module.plan(
                ctx, req.question, tenant_id=tenant_id, model=req.model, on_event=on_event,
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
            outcome_payload = serialize_outcome(plan_result.terminal_outcome)
            yield sse_format("done", outcome_payload)
            _log_safely(plan_result.terminal_outcome, tenant_id, model_name)
            _save_turn_safely(req.conversation_id, tenant_id, req.question, outcome_payload)
            return

        if plan_result.plan.confidence == "high":
            # Medium/low always pause. A future cost-based trigger even
            # at high confidence is deferred — see README.
            def do_finish(on_event):
                return answer_module.finish(
                    plan_result, tenant_id=tenant_id, on_event=on_event, catalog=ctx.catalog
                )

            async for kind, subkind, payload in run_with_events(do_finish):
                if kind == "progress":
                    yield sse_format(subkind, payload)
                elif kind == "error":
                    yield sse_format("error", payload)
                elif kind == "result":
                    outcome_payload = serialize_outcome(payload)
                    yield sse_format("done", outcome_payload)
                    _log_safely(payload, tenant_id, model_name)
                    _save_turn_safely(req.conversation_id, tenant_id, req.question, outcome_payload)
            return

        plan_id = str(uuid.uuid4())
        request.app.state.pending_plans[plan_id] = PendingPlan(
            plan_result=plan_result, tenant_id=tenant_id,
            question=req.question, conversation_id=req.conversation_id,
        )
        yield sse_format("awaiting_approval", {"plan_id": plan_id, **serialize_plan_result(plan_result)})

    return StreamingResponse(gen(), media_type="text/event-stream")


def _claim_pending(request: Request, plan_id: str) -> PendingPlan:
    """Pops a pending plan. Single use: approving or rejecting consumes it,
    so a replayed plan_id is simply unknown."""
    pending = request.app.state.pending_plans.pop(plan_id, None)
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown or already-resolved plan_id"
        )
    return pending


@router.post("/api/query/approve")
async def approve(req: ApproveRequest, request: Request) -> StreamingResponse:
    ctx = request.app.state.ctx
    pending = _claim_pending(request, req.plan_id)
    plan_result = pending.plan_result

    edited = req.sql is not None and req.sql.strip() != (plan_result.safe_sql or "").strip()
    model_name = _default_model_name()
    tenant_id = pending.tenant_id

    async def gen():
        def do_finish(on_event):
            return answer_module.finish(
                plan_result, tenant_id=tenant_id, on_event=on_event,
                override_sql=req.sql, catalog=ctx.catalog,
            )

        async for kind, subkind, payload in run_with_events(do_finish):
            if kind == "progress":
                yield sse_format(subkind, payload)
            elif kind == "error":
                yield sse_format("error", payload)
            elif kind == "result":
                outcome_payload = serialize_outcome(payload)
                yield sse_format("done", outcome_payload)
                _log_safely(payload, tenant_id, model_name, edited=edited)
                _save_turn_safely(pending.conversation_id, tenant_id, pending.question, outcome_payload)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/api/query/{plan_id}/reject")
async def reject(plan_id: str, request: Request) -> dict:
    # Not logged to audit.query_log — a discarded plan is lower-stakes
    # than the table's five real verdicts (see README).
    _claim_pending(request, plan_id)
    return {"status": "discarded"}


def _default_model_name() -> str:
    """What goes in the audit log when the caller didn't name a model.
    Provider-aware, so the log says which model actually answered."""
    from ..llm.client import default_model_for

    return default_model_for()
