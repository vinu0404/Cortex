import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from celery_app import celery_app
from app.common.retry import async_http_request_with_retry, async_redis_call
from config.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _DocContext:
    id: UUID
    kb_id: UUID
    user_id: UUID
    filename: str
    content_type: str
    storage_key: str | None
    staging_path: str | None
    source_url: str | None


# ---------------------------------------------------------------------------
# Source 1: device upload
# ---------------------------------------------------------------------------

class _DocTaskBase(celery_app.Task):
    abstract = True

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        doc_id = args[0] if args else kwargs.get("doc_id")
        if isinstance(exc, ValueError) and "not found" in str(exc).lower():
            logger.info("process_document_task: record deleted before task ran, doc_id=%s", doc_id)
            return
        logger.error("Document processing permanently failed: doc_id=%s err=%s", doc_id, exc)
        if doc_id:
            asyncio.run(_set_doc_failed(doc_id, str(exc)))


@celery_app.task(
    bind=True,
    base=_DocTaskBase,
    max_retries=settings.LLM_MAX_RETRIES,
    acks_late=True,
    soft_time_limit=300,
    time_limit=360,
    name="document_pipeline.tasks.process_document_task",
)
def process_document_task(self, doc_id: str) -> None:
    try:
        asyncio.run(_run_device_pipeline(doc_id))
    except Exception as exc:
        logger.error(
            "process_document_task retry: doc_id=%s attempt=%d err=%s",
            doc_id, self.request.retries + 1, exc,
        )
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# Source 2: S3 URL ingestion
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True,
    base=_DocTaskBase,
    max_retries=settings.LLM_MAX_RETRIES,
    acks_late=True,
    soft_time_limit=300,
    time_limit=360,
    name="document_pipeline.tasks.ingest_from_s3_task",
)
def ingest_from_s3_task(self, doc_id: str, s3_url: str, creds: dict) -> None:
    try:
        asyncio.run(_run_s3_pipeline(doc_id, s3_url, creds))
    except Exception as exc:
        logger.error("ingest_from_s3_task retry: doc_id=%s err=%s", doc_id, exc)
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# Pipeline implementations
# ---------------------------------------------------------------------------

async def _run_device_pipeline(doc_id: str) -> None:
    import redis.asyncio as aioredis

    # Always close redis on both success and failure paths
    redis_client = aioredis.from_url(settings.REDIS_URL)
    try:
        doc = await _load_doc_context(doc_id)
        if doc is None:
            return

        kb_id = str(doc.kb_id)
        user_id = str(doc.user_id)
        storage_key = doc.storage_key

        if doc.staging_path:
            from document_pipeline.storage import build_kb_storage_key, multipart_upload_file
            storage_key = build_kb_storage_key(kb_id, doc_id, doc.filename)
            await _mark_doc_status(doc_id, "uploading")
            await _publish_status(redis_client, user_id, kb_id, doc_id, "uploading", doc.filename)
            multipart_upload_file(doc.staging_path, storage_key, doc.content_type)
            _cleanup_staging_file(doc.staging_path)
            await _set_doc_storage(doc_id, storage_key)

        await _mark_doc_status(doc_id, "processing")
        await _publish_status(redis_client, user_id, kb_id, doc_id, "processing", doc.filename)
        await _ingest_from_storage(doc, storage_key, redis_client)
    finally:
        await redis_client.aclose()


async def _run_s3_pipeline(doc_id: str, s3_url: str, creds: dict) -> None:
    import redis.asyncio as aioredis

    # Always close redis
    redis_client = aioredis.from_url(settings.REDIS_URL)
    try:
        doc = await _load_doc_context(doc_id)
        if doc is None:
            return

        kb_id = str(doc.kb_id)
        user_id = str(doc.user_id)
        await _mark_doc_status(doc_id, "uploading")
        await _publish_status(redis_client, user_id, kb_id, doc_id, "uploading", doc.filename)

        file_bytes = await _download_from_s3(s3_url, creds)
        from document_pipeline.storage import build_kb_storage_key, multipart_upload_bytes
        storage_key = build_kb_storage_key(kb_id, doc_id, doc.filename)
        multipart_upload_bytes(file_bytes, storage_key, doc.content_type)
        await _set_doc_storage(doc_id, storage_key)

        await _mark_doc_status(doc_id, "processing")
        await _publish_status(redis_client, user_id, kb_id, doc_id, "processing", doc.filename)
        await _ingest_from_storage(doc, storage_key, redis_client)
    finally:
        await redis_client.aclose()


async def _ingest_from_storage(doc: _DocContext, storage_key: str | None, redis_client) -> None:
    """Common pipeline: download → parse → chunk → embed → Qdrant → ready."""
    from document_pipeline.parsers import parse_document
    from document_pipeline.chunker import chunk_document
    from document_pipeline.embedder import embed_texts
    from document_pipeline import vector_store

    if not storage_key:
        raise RuntimeError("Document has no storage key")

    openai_api_key = await _get_user_api_key(doc.user_id)
    from document_pipeline.storage import download_file
    file_bytes = download_file(storage_key)

    # parse_document is now async (was asyncio.run inside async context)
    raw_chunks = await parse_document(file_bytes, doc.filename, openai_api_key)
    if not raw_chunks:
        raise RuntimeError("Parser produced no chunks — document may be empty or unreadable")

    chunks = chunk_document(raw_chunks)
    if not chunks:
        raise RuntimeError("Chunker produced no chunks")

    texts = [c.text for c in chunks]
    embeddings = await embed_texts(texts, openai_api_key)

    kb_id = str(doc.kb_id)
    doc_id = str(doc.id)
    await vector_store.ensure_collection(kb_id, redis_client)
    await vector_store.create_text_index(kb_id)
    await vector_store.upsert_chunks(kb_id, doc_id, doc.filename, chunks, embeddings)

    if not await _mark_doc_ready(doc.id, len(chunks)):
        return
    await _publish_status(
        redis_client,
        str(doc.user_id),
        str(doc.kb_id),
        str(doc.id),
        "ready",
        doc.filename,
        chunk_count=len(chunks),
    )


async def _load_doc_context(doc_id: str) -> _DocContext | None:
    from sqlalchemy import select

    from app.knowledge_bases.db_models import KbDocument, KbProcessingStatusEnum
    from database.session import get_custom_db_context_session

    async with get_custom_db_context_session() as db:
        result = await db.execute(select(KbDocument).where(KbDocument.id == UUID(doc_id)))
        doc = result.scalar_one_or_none()
        if doc is None:
            raise ValueError(f"Document {doc_id} not found in DB — not retrying")
        if doc.processing_status == KbProcessingStatusEnum.cancelled:
            logger.info("document pipeline skipped — document %s was cancelled", doc_id)
            return None
        return _DocContext(
            id=doc.id,
            kb_id=doc.kb_id,
            user_id=doc.user_id,
            filename=doc.filename,
            content_type=doc.content_type or "application/octet-stream",
            storage_key=doc.storage_key,
            staging_path=doc.staging_path,
            source_url=doc.source_url,
        )


async def _mark_doc_status(doc_id: str, status: str) -> None:
    from sqlalchemy import select

    from app.knowledge_bases.db_models import KbDocument, KbProcessingStatusEnum
    from database.model_service import AsyncModelService
    from database.session import get_custom_db_context_session

    async with get_custom_db_context_session() as db:
        doc = await db.scalar(select(KbDocument).where(KbDocument.id == UUID(doc_id)))
        if doc:
            if doc.processing_status == KbProcessingStatusEnum.cancelled:
                return
            doc.processing_status = KbProcessingStatusEnum(status)
            await AsyncModelService(db).save_changes()


async def _set_doc_storage(doc_id: str, storage_key: str) -> None:
    from sqlalchemy import select

    from app.knowledge_bases.db_models import KbDocument
    from database.model_service import AsyncModelService
    from database.session import get_custom_db_context_session

    async with get_custom_db_context_session() as db:
        doc = await db.scalar(select(KbDocument).where(KbDocument.id == UUID(doc_id)))
        if doc:
            doc.storage_key = storage_key
            doc.staging_path = None
            await AsyncModelService(db).save_changes()


async def _get_user_api_key(user_id: UUID) -> str:
    from sqlalchemy import select

    from app.api_keys.db_models import UserApiKey
    from app.connectors.encryption import decrypt_str
    from database.session import get_custom_db_context_session

    async with get_custom_db_context_session() as db:
        key_rec = await db.scalar(select(UserApiKey).where(UserApiKey.user_id == user_id).limit(1))
        if not key_rec:
            raise RuntimeError("No API key found for user — cannot embed")
        return decrypt_str(key_rec.encrypted_key)


async def _mark_doc_ready(doc_id: UUID, chunk_count: int) -> bool:
    from sqlalchemy import select, update

    from app.knowledge_bases.db_models import KbDocument, KbProcessingStatusEnum, KnowledgeBase
    from database.model_service import AsyncModelService
    from database.session import get_custom_db_context_session

    async with get_custom_db_context_session() as db:
        doc = await db.scalar(select(KbDocument).where(KbDocument.id == doc_id))
        if not doc:
            raise ValueError(f"Document {doc_id} not found while marking ready")
        if doc.processing_status == KbProcessingStatusEnum.cancelled:
            logger.info("document pipeline skipped ready update — document %s was cancelled", doc_id)
            return False
        doc.processing_status = KbProcessingStatusEnum.ready
        doc.chunk_count = chunk_count
        doc.embedding_model = settings.KB_EMBEDDING_MODEL
        doc.indexed_at = datetime.now(timezone.utc)
        doc.error_message = None
        await db.execute(
            update(KnowledgeBase)
            .where(KnowledgeBase.id == doc.kb_id)
            .values(document_count=KnowledgeBase.document_count + 1)
        )
        await AsyncModelService(db).save_changes()
        return True


def _cleanup_staging_file(staging_path: str) -> None:
    try:
        staging_dir = os.path.dirname(staging_path)
        os.remove(staging_path)
        shutil.rmtree(staging_dir, ignore_errors=True)
    except Exception:
        logger.error("Could not remove staging file: %s", staging_path)


async def _download_from_s3(s3_url: str, creds: dict) -> bytes:
    if creds.get("access_key_id"):
        import boto3
        from botocore.config import Config
        from urllib.parse import urlparse
        parsed = urlparse(s3_url)
        path_parts = parsed.path.lstrip("/").split("/", 1)
        bucket = path_parts[0] if path_parts else ""
        key = path_parts[1] if len(path_parts) > 1 else ""
        region = creds.get("region", "us-east-1")
        client = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=creds["access_key_id"],
            aws_secret_access_key=creds["secret_access_key"],
            config=Config(signature_version="s3v4"),
        )
        resp = client.get_object(Bucket=bucket, Key=key)
        return resp["Body"].read()
    else:
        import httpx
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            resp = await async_http_request_with_retry(client, "GET", s3_url)
            return resp.content


async def _set_doc_failed(doc_id: str, error_message: str) -> None:
    from database.session import get_custom_db_context_session
    from database.model_service import AsyncModelService
    from app.knowledge_bases.db_models import KbDocument, KbProcessingStatusEnum
    from sqlalchemy import select
    import redis.asyncio as aioredis

    redis_client = aioredis.from_url(settings.REDIS_URL)
    try:
        async with get_custom_db_context_session() as db:
            result = await db.execute(select(KbDocument).where(KbDocument.id == UUID(doc_id)))
            doc = result.scalar_one_or_none()
            if doc:
                # Don't overwrite cancelled status with failed
                if doc.processing_status == KbProcessingStatusEnum.cancelled:
                    return

                if doc.staging_path and os.path.exists(doc.staging_path):
                    try:
                        staging_dir = os.path.dirname(doc.staging_path)
                        os.remove(doc.staging_path)
                        shutil.rmtree(staging_dir, ignore_errors=True)
                    except Exception as exc:
                        logger.warning("Failed to clean staged document %s: %s", doc.staging_path, exc)

                doc.processing_status = KbProcessingStatusEnum.failed
                doc.error_message = error_message[:2000]
                await AsyncModelService(db).save_changes()
                await _publish_status(
                    redis_client, str(doc.user_id), str(doc.kb_id), doc_id, "failed", doc.filename,
                    error_message=error_message[:200],
                )
    except Exception:
        logger.exception("Failed to update doc status to failed: doc_id=%s", doc_id)
    finally:
        await redis_client.aclose()


# added kb_id to payload so frontend SSE listener can identify which KB to refresh
async def _publish_status(
    redis_client,
    user_id: str,
    kb_id: str,
    doc_id: str,
    status: str,
    filename: str,
    chunk_count: int | None = None,
    error_message: str | None = None,
) -> None:
    import json
    payload: dict = {"kb_id": kb_id, "doc_id": doc_id, "status": status, "filename": filename}
    if chunk_count is not None:
        payload["chunk_count"] = chunk_count
    if error_message:
        payload["error_message"] = error_message
    try:
        await async_redis_call(redis_client, "publish", f"kb_status:{user_id}", json.dumps(payload))
    except Exception:
        logger.error("Failed to publish KB status: %s", payload)
