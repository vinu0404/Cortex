import asyncio
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
from database.session import get_db, get_custom_db_context_session
from document_pipeline.storage import generate_presigned_url

router = APIRouter()
logger = logging.getLogger(__name__)


def _normalize_sources(sources: list[dict] | None) -> list[dict] | None:
    if not sources:
        return sources
    normalized: list[dict] = []
    for source in sources:
        if not isinstance(source, dict):
            normalized.append(source)
            continue
        item = dict(source)
        legacy_collection_id = item.pop("collection_id", None)
        if legacy_collection_id and not item.get("web_collection_id"):
            item["web_collection_id"] = legacy_collection_id
        normalized.append(item)
    return normalized


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
            resp.sources = _normalize_sources(resp.sources)
            result.append(resp.model_dump(mode="json"))

        prev_cursor = encode_cursor(messages[0].created_at, messages[0].id) if has_more and messages else None
        return ok({"messages": result, "has_more": has_more, "prev_cursor": prev_cursor})
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.delete("/chat/conversations/{conversation_id}", response_model=None)
async def delete_conversation(
    conversation_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        await ChatManager(db).delete_conversation(conversation_id, current_user.id)
        return ok(message="Conversation deleted")
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.post("/chat/artifacts", response_model=None)
async def save_artifact(
    body: ArtifactSaveRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        saved, created = await ChatManager(db).save_user_artifact(body, current_user.id)
        return ok(saved.model_dump(mode="json"), status_code=201 if created else 200)
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
        retry_from=body.retry_from,
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
        return ok(await ChatManager(db).respond_to_hitl(
            body.request_id,
            current_user.id,
            body.approved,
            body.instructions,
        ))
    except AppError as e:
        return fail(e.code, e.message, e.status_code)
