import json
import logging
from collections.abc import AsyncGenerator
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.db_models import User
from app.auth.dependencies import get_current_user
from app.common.api_response import fail, ok
from app.common.exceptions import AppError
from app.common.pagination import build_cursor_page, decode_cursor
from app.vinu.builder import build_workspace
from app.vinu.manager import VinuConversationManager, prepare_chat_response
from app.vinu.models import (
    VinuBuildRequest,
    VinuChatRequest,
    VinuConversationResponse,
    VinuSettingsResponse,
    VinuSettingsUpdate,
)
from database.session import get_custom_db_context_session, get_db

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@router.get("/settings", response_model=None)
async def get_settings_endpoint(
    current_user: User = Depends(get_current_user),
) -> JSONResponse:
    try:
        return ok(VinuSettingsResponse.model_validate(current_user).model_dump(mode="json"))
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.patch("/settings", response_model=None)
async def update_settings_endpoint(
    body: VinuSettingsUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        await VinuConversationManager(db).update_agent_name(current_user.id, body.vinu_agent_name)
        return ok({"vinu_agent_name": body.vinu_agent_name})
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

@router.get("/conversations", response_model=None)
async def list_conversations(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        cursor_created_at, cursor_id = decode_cursor(cursor) if cursor else (None, None)
        items = await VinuConversationManager(db).list_conversations(
            current_user.id, limit, cursor_created_at, cursor_id
        )
        page = build_cursor_page(items, limit)
        return ok({
            "items": [VinuConversationResponse.model_validate(c).model_dump(mode="json") for c in page.items],
            "next_cursor": page.next_cursor,
            "has_next": page.has_next,
        })
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.delete("/conversations/{conv_id}", response_model=None)
async def delete_conversation(
    conv_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        await VinuConversationManager(db).delete_conversation(conv_id, current_user.id)
        return ok({"deleted": True})
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.get("/conversations/{conv_id}/messages", response_model=None)
async def get_conversation_messages(
    conv_id: UUID,
    cursor: str | None = Query(None),
    limit: int = Query(30, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        mgr = VinuConversationManager(db)
        conv = await mgr.get_conversation(conv_id, current_user.id)
        cursor_created_at, cursor_id = decode_cursor(cursor) if cursor else (None, None)
        items = await mgr.list_messages_paginated(conv_id, limit, cursor_created_at, cursor_id)
        page = build_cursor_page(items, limit)
        return ok({
            "messages": [{"role": m.role.value, "content": m.content} for m in page.items],
            "next_cursor": page.next_cursor,
            "has_more": page.has_next,
            "last_plan": conv.last_plan,
            "last_build": conv.last_build,
        })
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


# ---------------------------------------------------------------------------
# Chat (SSE)
# ---------------------------------------------------------------------------

@router.post("/chat")
async def vinu_chat(
    body: VinuChatRequest,
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    return StreamingResponse(
        _vinu_chat_stream(body, current_user),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Build (SSE)
# ---------------------------------------------------------------------------

@router.post("/build")
async def vinu_build(
    body: VinuBuildRequest,
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    async def _stream():
        async for chunk in build_workspace(current_user, body.plan):
            yield chunk
            if body.conversation_id and chunk.startswith("event: done\n"):
                data_line = chunk.split("data: ", 1)[1].strip()
                try:
                    result = json.loads(data_line)
                    async with get_custom_db_context_session() as save_db:
                        await VinuConversationManager(save_db).save_build(
                            body.conversation_id, result
                        )
                except Exception:
                    logger.exception("Failed to save build result to conversation")

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Chat stream — HTTP concern only; all business logic in prepare_chat_response
# ---------------------------------------------------------------------------

async def _vinu_chat_stream(body: VinuChatRequest, user: User) -> AsyncGenerator[str, None]:
    try:
        r = await prepare_chat_response(body, user)
    except Exception as exc:
        logger.error("Vinu chat preparation failed: %s", exc, exc_info=True)
        yield f"event: error\ndata: {json.dumps({'message': str(exc)})}\n\n"
        return

    yield f"event: meta\ndata: {json.dumps({'phase': r.phase, 'conversation_id': str(r.conv_id)})}\n\n"
    if r.was_compressed:
        yield f"event: compacting\ndata: {json.dumps({'message': 'Summarising earlier conversation…'})}\n\n"
    for char in r.reply:
        yield f"data: {json.dumps(char)}\n\n"
    if r.plan:
        yield f"event: plan\ndata: {json.dumps(r.plan)}\n\n"
    if r.questions:
        yield f"event: clarification_required\ndata: {json.dumps({'questions': r.questions})}\n\n"
    if r.new_title:
        yield f"event: conversation_title\ndata: {json.dumps({'title': r.new_title, 'conversation_id': str(r.conv_id)})}\n\n"
    yield f"event: done\ndata: {json.dumps({'phase': r.phase})}\n\n"
