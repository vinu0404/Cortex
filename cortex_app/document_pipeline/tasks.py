import asyncio
import logging
import os
import shutil
from datetime import datetime, timezone
from uuid import UUID

from celery_app import celery_app
from config.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Source 1: device upload
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True,
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
        logger.warning(
            "process_document_task retry: doc_id=%s attempt=%d err=%s",
            doc_id, self.request.retries + 1, exc,
        )
        raise self.retry(exc=exc)


# BUG-03: on_failure must NOT have self as first param — args are (exc, task_id, args, kwargs, einfo)
@process_document_task.on_failure
def on_process_document_failure(exc, task_id, args, kwargs, einfo):
    doc_id = args[0] if args else kwargs.get("doc_id")
    logger.error("Document processing permanently failed: doc_id=%s err=%s", doc_id, exc)
    if doc_id:
        asyncio.run(_set_doc_failed(doc_id, str(exc)))


# ---------------------------------------------------------------------------
# Source 2: S3 URL ingestion
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True,
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
        logger.warning("ingest_from_s3_task retry: doc_id=%s err=%s", doc_id, exc)
        raise self.retry(exc=exc)


# BUG-03: same fix — no self param
@ingest_from_s3_task.on_failure
def on_ingest_s3_failure(exc, task_id, args, kwargs, einfo):
    doc_id = args[0] if args else kwargs.get("doc_id")
    logger.error("S3 ingestion permanently failed: doc_id=%s err=%s", doc_id, exc)
    if doc_id:
        asyncio.run(_set_doc_failed(doc_id, str(exc)))


# ---------------------------------------------------------------------------
# Pipeline implementations
# ---------------------------------------------------------------------------

async def _run_device_pipeline(doc_id: str) -> None:
    from database.session import get_custom_db_context_session
    from app.knowledge_bases.db_models import KbDocument, KbProcessingStatusEnum
    from sqlalchemy import select
    import redis.asyncio as aioredis

    # BUG-07: always close redis on both success and failure paths
    redis_client = aioredis.from_url(settings.REDIS_URL)
    try:
        async with get_custom_db_context_session() as db:
            result = await db.execute(select(KbDocument).where(KbDocument.id == UUID(doc_id)))
            # BUG-02: scalar_one() raises NoResultFound if doc missing → blindly retries forever
            doc = result.scalar_one_or_none()
            if doc is None:
                raise ValueError(f"Document {doc_id} not found in DB — not retrying")

            kb_id = str(doc.kb_id)
            user_id = str(doc.user_id)
            staging_path = doc.staging_path
            filename = doc.filename
            content_type = doc.content_type or "application/octet-stream"

            # BUG-12: use canonical build_kb_storage_key instead of inline f-string
            from document_pipeline.storage import multipart_upload_file, build_kb_storage_key
            storage_key = build_kb_storage_key(kb_id, doc_id, filename)

            # --- 1. uploading ---
            doc.processing_status = KbProcessingStatusEnum.uploading
            await db.commit()
            await _publish_status(redis_client, user_id, kb_id, doc_id, "uploading", filename)

            multipart_upload_file(staging_path, storage_key, content_type)

            # Delete staging file
            try:
                staging_dir = os.path.dirname(staging_path)
                os.remove(staging_path)
                shutil.rmtree(staging_dir, ignore_errors=True)
            except Exception:
                logger.warning("Could not remove staging file: %s", staging_path)

            doc.storage_key = storage_key
            doc.staging_path = None
            await db.commit()

            # --- 2. processing ---
            doc.processing_status = KbProcessingStatusEnum.processing
            await db.commit()
            await _publish_status(redis_client, user_id, kb_id, doc_id, "processing", filename)

            await _ingest_from_storage(db, doc, kb_id, doc_id, filename, storage_key, user_id, redis_client)
    finally:
        await redis_client.aclose()


async def _run_s3_pipeline(doc_id: str, s3_url: str, creds: dict) -> None:
    from database.session import get_custom_db_context_session
    from app.knowledge_bases.db_models import KbDocument, KbProcessingStatusEnum
    from sqlalchemy import select
    import redis.asyncio as aioredis

    # BUG-07: always close redis
    redis_client = aioredis.from_url(settings.REDIS_URL)
    try:
        async with get_custom_db_context_session() as db:
            result = await db.execute(select(KbDocument).where(KbDocument.id == UUID(doc_id)))
            # BUG-02: scalar_one() raises NoResultFound if doc missing
            doc = result.scalar_one_or_none()
            if doc is None:
                raise ValueError(f"Document {doc_id} not found in DB — not retrying")

            kb_id = str(doc.kb_id)
            user_id = str(doc.user_id)
            filename = doc.filename

            # --- 1. uploading (download from S3 source) ---
            doc.processing_status = KbProcessingStatusEnum.uploading
            await db.commit()
            await _publish_status(redis_client, user_id, kb_id, doc_id, "uploading", filename)

            file_bytes = await _download_from_s3(s3_url, creds)

            # BUG-12: use canonical build_kb_storage_key
            from document_pipeline.storage import multipart_upload_bytes, build_kb_storage_key
            storage_key = build_kb_storage_key(kb_id, doc_id, filename)
            content_type = doc.content_type or "application/octet-stream"

            multipart_upload_bytes(file_bytes, storage_key, content_type)

            doc.storage_key = storage_key
            await db.commit()

            # --- 2. processing ---
            doc.processing_status = KbProcessingStatusEnum.processing
            await db.commit()
            await _publish_status(redis_client, user_id, kb_id, doc_id, "processing", filename)

            await _ingest_from_storage(db, doc, kb_id, doc_id, filename, storage_key, user_id, redis_client)
    finally:
        await redis_client.aclose()


async def _ingest_from_storage(db, doc, kb_id: str, doc_id: str, filename: str,
                                storage_key: str, user_id: str, redis_client) -> None:
    """Common pipeline: download → parse → chunk → embed → Qdrant → ready."""
    from app.knowledge_bases.db_models import KbProcessingStatusEnum, KnowledgeBase
    from document_pipeline.parsers import parse_document
    from document_pipeline.chunker import chunk_document
    from document_pipeline.embedder import embed_texts
    from document_pipeline import vector_store
    from sqlalchemy import select, update

    # Fetch user's API key (first available)
    from app.api_keys.db_models import UserApiKey
    key_result = await db.execute(
        select(UserApiKey).where(UserApiKey.user_id == doc.user_id).limit(1)
    )
    key_rec = key_result.scalar_one_or_none()
    if not key_rec:
        raise RuntimeError("No API key found for user — cannot embed")

    from app.connectors.encryption import decrypt_str
    openai_api_key = decrypt_str(key_rec.encrypted_key)

    from document_pipeline.storage import download_file
    file_bytes = download_file(storage_key)

    # BUG-01: parse_document is now async (was asyncio.run inside async context)
    raw_chunks = await parse_document(file_bytes, filename, openai_api_key)
    if not raw_chunks:
        raise RuntimeError("Parser produced no chunks — document may be empty or unreadable")

    chunks = chunk_document(raw_chunks)
    if not chunks:
        raise RuntimeError("Chunker produced no chunks")

    texts = [c.text for c in chunks]
    embeddings = await embed_texts(texts, openai_api_key)

    await vector_store.ensure_collection(kb_id)
    await vector_store.create_text_index(kb_id)
    await vector_store.upsert_chunks(kb_id, doc_id, filename, chunks, embeddings)

    doc.processing_status = KbProcessingStatusEnum.ready
    doc.chunk_count = len(chunks)
    doc.embedding_model = settings.KB_EMBEDDING_MODEL
    doc.indexed_at = datetime.now(timezone.utc)
    doc.error_message = None

    # BUG-21: increment knowledge_base document_count atomically
    await db.execute(
        update(KnowledgeBase)
        .where(KnowledgeBase.id == doc.kb_id)
        .values(document_count=KnowledgeBase.document_count + 1)
    )

    await db.commit()
    await _publish_status(redis_client, user_id, kb_id, doc_id, "ready", filename, chunk_count=len(chunks))


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
            resp = await client.get(s3_url)
            resp.raise_for_status()
            return resp.content


async def _set_doc_failed(doc_id: str, error_message: str) -> None:
    from database.session import get_custom_db_context_session
    from app.knowledge_bases.db_models import KbDocument, KbProcessingStatusEnum
    from sqlalchemy import select
    import redis.asyncio as aioredis

    redis_client = aioredis.from_url(settings.REDIS_URL)
    try:
        async with get_custom_db_context_session() as db:
            result = await db.execute(select(KbDocument).where(KbDocument.id == UUID(doc_id)))
            doc = result.scalar_one_or_none()
            if doc:
                if doc.staging_path and os.path.exists(doc.staging_path):
                    try:
                        staging_dir = os.path.dirname(doc.staging_path)
                        os.remove(doc.staging_path)
                        shutil.rmtree(staging_dir, ignore_errors=True)
                    except Exception:
                        pass

                doc.processing_status = KbProcessingStatusEnum.failed
                doc.error_message = error_message[:2000]
                await db.commit()
                await _publish_status(
                    redis_client, str(doc.user_id), str(doc.kb_id), doc_id, "failed", doc.filename,
                    error_message=error_message[:200],
                )
    except Exception:
        logger.exception("Failed to update doc status to failed: doc_id=%s", doc_id)
    finally:
        await redis_client.aclose()


# BUG-06: added kb_id to payload so frontend SSE listener can identify which KB to refresh
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
        await redis_client.publish(f"kb_status:{user_id}", json.dumps(payload))
    except Exception:
        logger.error("Failed to publish KB status: %s", payload)
