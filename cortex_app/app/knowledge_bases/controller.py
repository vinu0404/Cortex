import asyncio
import json
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.db_models import User
from app.common.api_response import fail, ok
from app.common.exceptions import AppError
from app.common.redis_client import get_async_redis
from app.knowledge_bases.manager import KnowledgeBaseManager
from app.knowledge_bases.models import (
    KbDocumentResponse,
    KnowledgeBaseCreate,
    KnowledgeBaseResponse,
    RetryResponse,
    S3IngestRequest,
)
from config.settings import get_settings
from database.session import get_db

router = APIRouter()
logger = logging.getLogger(__name__)
settings = get_settings()


@router.post("/knowledge-bases", response_model=None)
async def create_kb(
    body: KnowledgeBaseCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        mgr = KnowledgeBaseManager(db)
        kb = await mgr.create_kb(current_user.id, body.name, body.description)
        return ok(KnowledgeBaseResponse.model_validate(kb).model_dump(mode="json"))
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.get("/knowledge-bases", response_model=None)
async def list_kbs(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        mgr = KnowledgeBaseManager(db)
        kbs = await mgr.list_kbs(current_user.id)
        return ok([KnowledgeBaseResponse.model_validate(kb).model_dump(mode="json") for kb in kbs])
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.delete("/knowledge-bases/{kb_id}", response_model=None)
async def delete_kb(
    kb_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        mgr = KnowledgeBaseManager(db)
        await mgr.delete_kb(kb_id, current_user.id)
        return ok({"deleted": True})
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.post("/knowledge-bases/{kb_id}/documents", response_model=None)
async def upload_documents(
    kb_id: UUID,
    files: list[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        file_tuples = []
        for f in files:
            content = await f.read()
            file_tuples.append((f.filename or "file", content, f.content_type or "application/octet-stream"))

        mgr = KnowledgeBaseManager(db)
        results = await mgr.upload_documents(kb_id, current_user.id, file_tuples)
        return ok(results)
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.post("/knowledge-bases/{kb_id}/documents/from-s3", response_model=None)
async def ingest_from_s3(
    kb_id: UUID,
    body: S3IngestRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        mgr = KnowledgeBaseManager(db)
        doc = await mgr.ingest_from_s3(
            kb_id=kb_id,
            user_id=current_user.id,
            url=body.url,
            filename=body.filename,
            access_key_id=body.access_key_id,
            secret_access_key=body.secret_access_key,
            region=body.region,
        )
        return ok({"doc_id": str(doc.id), "status": "pending"})
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.get("/knowledge-bases/{kb_id}/documents", response_model=None)
async def list_documents(
    kb_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        mgr = KnowledgeBaseManager(db)
        docs = await mgr.list_documents(kb_id, current_user.id)
        return ok([KbDocumentResponse.model_validate(d).model_dump(mode="json") for d in docs])
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.delete("/knowledge-bases/{kb_id}/documents/{doc_id}", response_model=None)
async def delete_document(
    kb_id: UUID,
    doc_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        mgr = KnowledgeBaseManager(db)
        await mgr.delete_document(kb_id, doc_id, current_user.id)
        return ok({"deleted": True})
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.post("/knowledge-bases/{kb_id}/documents/{doc_id}/retry", response_model=None)
async def retry_document(
    kb_id: UUID,
    doc_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        mgr = KnowledgeBaseManager(db)
        doc = await mgr.retry_document(kb_id, doc_id, current_user.id)
        return ok(RetryResponse(doc_id=doc.id, status="pending").model_dump(mode="json"))
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.get("/knowledge-bases/{kb_id}/documents/{doc_id}/view", response_model=None)
async def view_document(
    kb_id: UUID,
    doc_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        mgr = KnowledgeBaseManager(db)
        data = await mgr.get_presigned_url(kb_id, doc_id, current_user.id)
        return ok(data)
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.get("/knowledge-bases/status/stream")
async def kb_status_stream(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    from jose import JWTError, jwt
    from app.auth.manager import AuthManager
    from uuid import UUID as _UUID
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
        if payload.get("type") != "access":
            raise ValueError
        user_id_str = payload.get("sub")
        if not user_id_str:
            raise ValueError
        user = await AuthManager(db).get_user_by_id(_UUID(user_id_str))
        if not user or not user.is_active:
            raise ValueError
    except (JWTError, ValueError) as e:
        logger.error("SSE auth rejected: %s — %s", type(e).__name__, e)
        from fastapi.responses import Response
        return Response("Unauthorized", status_code=401)

    user_id = user.id

    async def event_generator():
        # BUG-05: get_async_redis() is a sync @lru_cache function — do NOT await it
        redis = get_async_redis()
        channel = f"kb_status:{user_id}"
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel)
        try:
            yield "data: connected\n\n"
            async for message in pubsub.listen():
                if message["type"] == "message":
                    # BUG-11: decode_responses=True means data is already str — no .decode()
                    yield f"data: {message['data']}\n\n"
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    return StreamingResponse(event_generator(), media_type="text/event-stream")
