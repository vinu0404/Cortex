import json
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.db_models import User
from app.chat.manager import ChatManager
from app.chat.models import (
    ChatStreamRequest,
    ConversationCreate,
    ConversationResponse,
    HitlRespondRequest,
    MessageResponse,
)
from app.chat.streaming import chat_stream
from app.common.api_response import fail, ok
from app.common.exceptions import AppError
from app.common.pagination import build_cursor_page, decode_cursor
from app.common.redis_client import get_async_redis
from database.session import get_db

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/chat/conversations", response_model=None)
async def create_conversation(
    body: ConversationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        chat_mgr = ChatManager(db)
        conv = await chat_mgr.create_conversation(body.workspace_id, current_user.id)
        return ok(ConversationResponse.model_validate(conv).model_dump(mode="json"), status_code=201)
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.get("/chat/conversations", response_model=None)
async def list_conversations(
    workspace_id: UUID = Query(...),
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        cursor_created_at, cursor_id = decode_cursor(cursor) if cursor else (None, None)
        chat_mgr = ChatManager(db)
        items = await chat_mgr.list_conversations(
            workspace_id, current_user.id, limit, cursor_created_at, cursor_id
        )
        page = build_cursor_page(items, limit)
        return ok({
            "items": [ConversationResponse.model_validate(c).model_dump(mode="json") for c in page.items],
            "next_cursor": page.next_cursor,
            "has_next": page.has_next,
        })
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.get("/chat/conversations/{conversation_id}/messages", response_model=None)
async def get_messages(
    conversation_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        chat_mgr = ChatManager(db)
        messages = await chat_mgr.get_messages(conversation_id, current_user.id)
        return ok([MessageResponse.model_validate(m).model_dump(mode="json") for m in messages])
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.post("/chat/stream", response_class=StreamingResponse)
async def stream_chat(
    request: Request,
    body: ChatStreamRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    # Create conversation if not provided
    chat_mgr = ChatManager(db)
    if not body.conversation_id:
        conv = await chat_mgr.create_conversation(body.workspace_id, current_user.id)
        conversation_id = conv.id
    else:
        conversation_id = body.conversation_id

    generator = chat_stream(
        request=request,
        workspace_id=body.workspace_id,
        conversation_id=conversation_id,
        query=body.query,
        user_id=current_user.id,
        persona_id=body.persona_id,
        db=db,
    )

    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/hitl/respond", response_model=None)
async def hitl_respond(
    body: HitlRespondRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        chat_mgr = ChatManager(db)
        await chat_mgr.resolve_hitl_request(
            body.request_id, current_user.id, body.approved, body.instructions
        )
        await db.commit()  # commit before publish so SSE stream sees updated status
        redis = get_async_redis()
        payload = json.dumps({
            "approved": body.approved,
            "instructions": body.instructions,
            "request_id": str(body.request_id),
        })
        await redis.publish(f"hitl:{body.request_id}", payload)
        return ok({"approved": body.approved})
    except AppError as e:
        return fail(e.code, e.message, e.status_code)
