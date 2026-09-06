"""Conversation history routes.

There are no accounts, so these are open and every thread is visible to
anyone who can reach the instance. Reads are still scoped by tenant, and
the tenant comes from server configuration (`app.tenant.current_tenant_id`)
rather than from anything the caller sends.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from ..tenant import current_tenant_id
from . import store

router = APIRouter(prefix="/api/conversations", tags=["history"])


class CreateConversationRequest(BaseModel):
    title: str | None = None


class RenameRequest(BaseModel):
    title: str


@router.get("")
async def list_conversations() -> dict:
    convos = store.list_conversations(current_tenant_id())
    return {"conversations": [c.__dict__ for c in convos]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_conversation(req: CreateConversationRequest) -> dict:
    return store.create_conversation(current_tenant_id(), req.title).__dict__


@router.get("/{conversation_id}")
async def get_conversation(conversation_id: str) -> dict:
    messages = store.get_messages(conversation_id, current_tenant_id())
    if messages is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="conversation not found")
    return {"conversation_id": conversation_id, "messages": [m.__dict__ for m in messages]}


@router.patch("/{conversation_id}")
async def rename_conversation(conversation_id: str, req: RenameRequest) -> dict:
    if not store.rename_conversation(conversation_id, current_tenant_id(), req.title):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="conversation not found")
    return {"status": "renamed"}


@router.delete("/{conversation_id}")
async def delete_conversation(conversation_id: str) -> dict:
    if not store.delete_conversation(conversation_id, current_tenant_id()):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="conversation not found")
    return {"status": "deleted"}
