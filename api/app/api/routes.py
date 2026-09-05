"""HTTP surface. POST /api/query runs generate -> guard -> budget and
either auto-executes at "high" confidence, or pauses and streams an
`awaiting_approval` event for a human to read and approve. POST
/api/query/approve resumes a paused plan, optionally with human-edited
SQL, re-guarded exactly like model output.

Every route here requires authentication, and the tenant a query runs
under comes from the caller's signed token (`Principal.tenant_id`),
never from the request body. That was the whole point of adding auth:
the tenant filtering in db/02_roles.sql and the view predicates in
db/00_schema.sql were previously enforcing a number the caller chose.

Pending plans live in an in-memory dict (app.state.pending_plans), not
Postgres — a known gap, see README. They are keyed by plan_id and also
carry the owning principal, so one user cannot approve another's plan.
Terminal outcomes are persisted to audit.query_log, and to the caller's
conversation history when one is supplied.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..auth.deps import Principal, current_principal, require_operator
from ..history import store as history_store
from ..obs.audit import log_query_run
from ..obs.stats import get_recent_events, get_summary
from ..pipeline import answer as answer_module
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
    """A plan awaiting approval, plus who is allowed to approve it."""
    plan_result: object
    user_id: int
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


def _save_turn_safely(conversation_id, principal, question, outcome_payload) -> None:
    """History is a convenience, not the answer. A write failure here is
    logged and swallowed, same reasoning as the audit write."""
    if not conversation_id:
        return
    try:
        history_store.append_turn(
            conversation_id, principal.user_id, principal.tenant_id, question, outcome_payload
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[history] failed to save turn: {exc}")


@router.get("/api/health")
async def health(request: Request) -> dict:
    ctx = request.app.state.ctx
    return {"status": "ok", "schema_fingerprint": ctx.snapshot.fingerprint}


@router.get("/api/stats")
async def stats(hours: int = 24, _: Principal = Depends(require_operator)) -> dict:
    return get_summary(hours=hours)


@router.get("/api/audit")
async def audit(limit: int = 50, _: Principal = Depends(require_operator)) -> dict:
    return {"events": get_recent_events(limit=limit)}


@router.post("/api/query")
async def query(
    req: QueryRequest, request: Request, principal: Principal = Depends(current_principal)
) -> StreamingResponse:
    ctx = request.app.state.ctx
    model_name = req.model or _default_model_name()
    tenant_id = principal.tenant_id

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
            _save_turn_safely(req.conversation_id, principal, req.question, outcome_payload)
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
                    _save_turn_safely(req.conversation_id, principal, req.question, outcome_payload)
            return

        plan_id = str(uuid.uuid4())
        request.app.state.pending_plans[plan_id] = PendingPlan(
            plan_result=plan_result, user_id=principal.user_id, tenant_id=tenant_id,
            question=req.question, conversation_id=req.conversation_id,
        )
        yield sse_format("awaiting_approval", {"plan_id": plan_id, **serialize_plan_result(plan_result)})

    return StreamingResponse(gen(), media_type="text/event-stream")


def _claim_pending(request: Request, plan_id: str, principal: Principal) -> PendingPlan:
    """Pops a pending plan, but only for the principal who created it.
    A plan belonging to someone else is reported as unknown rather than
    forbidden — no reason to confirm that another user's plan_id exists."""
    pending = request.app.state.pending_plans.get(plan_id)
    if pending is None or pending.user_id != principal.user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown or already-resolved plan_id"
        )
    return request.app.state.pending_plans.pop(plan_id)


@router.post("/api/query/approve")
async def approve(
    req: ApproveRequest, request: Request, principal: Principal = Depends(current_principal)
) -> StreamingResponse:
    ctx = request.app.state.ctx
    pending = _claim_pending(request, req.plan_id, principal)
    plan_result = pending.plan_result

    edited = req.sql is not None and req.sql.strip() != (plan_result.safe_sql or "").strip()
    model_name = _default_model_name()
    tenant_id = principal.tenant_id

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
                _save_turn_safely(pending.conversation_id, principal, pending.question, outcome_payload)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/api/query/{plan_id}/reject")
async def reject(
    plan_id: str, request: Request, principal: Principal = Depends(current_principal)
) -> dict:
    # Not logged to audit.query_log — a discarded plan is lower-stakes
    # than the table's five real verdicts (see README).
    _claim_pending(request, plan_id, principal)
    return {"status": "discarded"}


def _default_model_name() -> str:
    from ..llm.client import DEFAULT_MODEL

    return DEFAULT_MODEL
