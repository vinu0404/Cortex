import asyncio
import base64
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
    ArtifactSaveRequest,
    ArtifactSaveResponse,
    ChatStreamRequest,
    ConversationCreate,
    ConversationResponse,
    HitlRespondRequest,
    MessageResponse,
    SavedArtifactResponse,
)
from app.chat.streaming import chat_stream
from app.common.api_response import fail, ok
from app.common.exceptions import AppError
from app.common.pagination import build_cursor_page, decode_cursor, encode_cursor
from app.common.redis_client import get_async_redis
from database.session import get_db, get_custom_db_context_session
from document_pipeline.storage import build_artifact_storage_key, generate_presigned_url, upload_bytes

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
    cursor: str | None = Query(None),
    limit: int = Query(30, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        cursor_created_at, cursor_id = decode_cursor(cursor) if cursor else (None, None)
        chat_mgr = ChatManager(db)
        messages, has_more = await chat_mgr.get_messages(
            conversation_id, current_user.id,
            cursor_created_at=cursor_created_at, cursor_id=cursor_id, limit=limit,
        )
        artifact_map = await chat_mgr.get_artifacts_for_messages([m.id for m in messages])

        loop = asyncio.get_running_loop()
        # Split: mermaid/code stored inline (content), PDF/CSV stored in B2 (presigned URL)
        _inline_types = {"mermaid", "code"}
        b2_artifacts = [(m.id, a) for m in messages for a in artifact_map.get(m.id, []) if a.type not in _inline_types]
        inline_artifacts = [(m.id, a) for m in messages for a in artifact_map.get(m.id, []) if a.type in _inline_types]

        urls = await asyncio.gather(*[
            loop.run_in_executor(None, generate_presigned_url, a.storage_key)
            for _, a in b2_artifacts
        ])
        url_map: dict[UUID, list[SavedArtifactResponse]] = {}
        for (msg_id, a), url in zip(b2_artifacts, urls):
            url_map.setdefault(msg_id, []).append(
                SavedArtifactResponse(id=a.id, type=a.type, title=a.title, filename=a.filename, url=url)
            )
        for msg_id, a in inline_artifacts:
            url_map.setdefault(msg_id, []).append(
                SavedArtifactResponse(id=a.id, type=a.type, title=a.title, filename=a.filename, content=a.content)
            )

        result = []
        for m in messages:
            resp = MessageResponse.model_validate(m)
            resp.saved_artifacts = url_map.get(m.id, [])
            result.append(resp.model_dump(mode="json"))

        prev_cursor = encode_cursor(messages[0].created_at, messages[0].id) if has_more and messages else None
        return ok({"messages": result, "has_more": has_more, "prev_cursor": prev_cursor})
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.post("/chat/artifacts", response_model=None)
async def save_artifact(
    body: ArtifactSaveRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        chat_mgr = ChatManager(db)
        await chat_mgr.get_conversation(body.conversation_id, current_user.id)

        # Return existing artifact if already saved (two-tab race guard)
        existing = await chat_mgr.get_artifact_by_message_and_type(body.message_id, body.type)
        if existing:
            loop = asyncio.get_running_loop()
            url = await loop.run_in_executor(None, generate_presigned_url, existing.storage_key)
            return ok(ArtifactSaveResponse(id=existing.id, url=url).model_dump(mode="json"))

        filename = body.filename
        if body.type == "pdf":
            raw_bytes = base64.b64decode(body.content)
            content_type = "application/pdf"
        else:
            raw_bytes = body.content.encode("utf-8")
            content_type = "text/csv"

        key = build_artifact_storage_key(str(body.conversation_id), filename)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, upload_bytes, raw_bytes, key, content_type)
        url = await loop.run_in_executor(None, generate_presigned_url, key)

        artifact = await chat_mgr.save_artifact(
            message_id=body.message_id,
            conversation_id=body.conversation_id,
            user_id=current_user.id,
            type=body.type,
            title=body.title,
            filename=filename,
            storage_key=key,
        )
        await db.commit()
        return ok(ArtifactSaveResponse(id=artifact.id, url=url).model_dump(mode="json"), status_code=201)
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.post("/chat/stream", response_class=StreamingResponse)
async def stream_chat(
    request: Request,
    body: ChatStreamRequest,
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    if not body.conversation_id:
        async with get_custom_db_context_session() as db:
            conv = await ChatManager(db).create_conversation(body.workspace_id, current_user.id)
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
        timezone=request.headers.get("X-Timezone", "UTC"),
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
