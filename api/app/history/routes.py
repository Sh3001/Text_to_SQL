"""Conversation history routes. Every one is scoped to the authenticated
principal — there is no route here that takes a user or tenant as input."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..auth.deps import Principal, current_principal
from . import store

router = APIRouter(prefix="/api/conversations", tags=["history"])


class CreateConversationRequest(BaseModel):
    title: str | None = None


class RenameRequest(BaseModel):
    title: str


@router.get("")
async def list_conversations(principal: Principal = Depends(current_principal)) -> dict:
    convos = store.list_conversations(principal.user_id, principal.tenant_id)
    return {"conversations": [c.__dict__ for c in convos]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_conversation(
    req: CreateConversationRequest, principal: Principal = Depends(current_principal)
) -> dict:
    convo = store.create_conversation(principal.user_id, principal.tenant_id, req.title)
    return convo.__dict__


@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: str, principal: Principal = Depends(current_principal)
) -> dict:
    messages = store.get_messages(conversation_id, principal.user_id, principal.tenant_id)
    if messages is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="conversation not found")
    return {"conversation_id": conversation_id, "messages": [m.__dict__ for m in messages]}


@router.patch("/{conversation_id}")
async def rename_conversation(
    conversation_id: str, req: RenameRequest, principal: Principal = Depends(current_principal)
) -> dict:
    if not store.rename_conversation(conversation_id, principal.user_id, principal.tenant_id, req.title):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="conversation not found")
    return {"status": "renamed"}


@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: str, principal: Principal = Depends(current_principal)
) -> dict:
    if not store.delete_conversation(conversation_id, principal.user_id, principal.tenant_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="conversation not found")
    return {"status": "deleted"}
