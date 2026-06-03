import asyncio
import logging
import uuid
from pathlib import Path
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.knowledge_bases.db_models import (
    AgentKnowledgeBase,
    KbDocument,
    KnowledgeBaseModelService,
    KbProcessingStatusEnum,
    KbSourceTypeEnum,
    KnowledgeBase,
)
from config.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

_CANCELLABLE_STATUSES = {
    KbProcessingStatusEnum.pending_upload,
    KbProcessingStatusEnum.pending,
    KbProcessingStatusEnum.uploading,
    KbProcessingStatusEnum.processing,
}

_REINDEXABLE_STATUSES = {
    KbProcessingStatusEnum.ready,
    KbProcessingStatusEnum.failed,
    KbProcessingStatusEnum.cancelled,
}


class KnowledgeBaseManager:
    def __init__(self, db: AsyncSession):
        self._db = db
        self._kb_model_service = KnowledgeBaseModelService(db)

    # -----------------------------------------------------------------------
    # Knowledge Bases
    # -----------------------------------------------------------------------

    async def list_kbs(self, user_id: UUID) -> list[KnowledgeBase]:
        return await self._kb_model_service.list_kbs(user_id)

    async def create_kb(self, user_id: UUID, name: str, description: str | None) -> KnowledgeBase:
        try:
            return await self._kb_model_service.create_kb(user_id, name, description)
        except IntegrityError as e:
            await self._kb_model_service.undo_changes()
            raise ConflictError(f'A knowledge base named "{name}" already exists.') from e

    async def delete_kb(self, kb_id: UUID, user_id: UUID) -> None:
        kb = await self._kb_model_service.get_kb(kb_id, user_id)
        docs = await self._kb_model_service.delete_kb(kb)
        await self._kb_model_service.save_changes()

        # B2 delete is sync boto3 — must run in executor to avoid blocking event loop
        loop = asyncio.get_running_loop()
        for doc in docs:
            if doc.storage_key:
                await loop.run_in_executor(None, _delete_b2_file, doc.storage_key)

        # await directly — asyncio.create_task is fire-and-forget, may never complete
        from document_pipeline import vector_store
        await vector_store.delete_collection(str(kb_id))

    # -----------------------------------------------------------------------
    # Documents
    # -----------------------------------------------------------------------

    async def list_documents(self, kb_id: UUID, user_id: UUID) -> list[KbDocument]:
        await self._kb_model_service.get_kb(kb_id, user_id)
        return await self._kb_model_service.list_documents(kb_id)

    async def presign_upload(
        self,
        kb_id: UUID,
        user_id: UUID,
        filename: str,
        content_type: str,
        file_size_bytes: int,
        file_hash: str | None = None,
    ):
        from app.knowledge_bases.models import PresignUploadResponse
        from document_pipeline.storage import build_kb_storage_key, generate_presigned_put_url

        await self._kb_model_service.get_kb(kb_id, user_id)

        ext = Path(filename).suffix.lower()
        if ext not in settings.KB_SUPPORTED_EXTENSIONS:
            raise ConflictError(f"Unsupported extension: {ext}")
        if file_size_bytes > settings.KB_MAX_FILE_SIZE_MB * 1024 * 1024:
            raise ConflictError(f"File too large. Max: {settings.KB_MAX_FILE_SIZE_MB} MB")

        # Hash-based duplicate / resume check
        if file_hash:
            existing = await self._kb_model_service.find_doc_by_hash(kb_id, file_hash)
            if existing:
                if existing.processing_status != KbProcessingStatusEnum.pending_upload:
                    return PresignUploadResponse(
                        status="already_exists",
                        doc_id=existing.id,
                        filename=existing.filename,
                    )
                # Interrupted upload — reissue presigned URL for same storage_key
                upload_url = generate_presigned_put_url(existing.storage_key, content_type)
                return PresignUploadResponse(
                    status="resumable",
                    doc_id=existing.id,
                    filename=existing.filename,
                    upload_url=upload_url,
                    storage_key=existing.storage_key,
                    expires_in=settings.B2_PRESIGN_EXPIRY,
                )

        doc_id = uuid.uuid4()
        storage_key = build_kb_storage_key(str(kb_id), str(doc_id), filename)

        doc = await self._kb_model_service.create_document(
            id=doc_id,
            kb_id=kb_id,
            user_id=user_id,
            filename=filename,
            file_size_bytes=file_size_bytes,
            content_type=content_type,
            storage_key=storage_key,
            file_hash=file_hash,
            staging_path=None,
            source_type=KbSourceTypeEnum.device,
            processing_status=KbProcessingStatusEnum.pending_upload,
        )
        await self._kb_model_service.save_changes()

        upload_url = generate_presigned_put_url(storage_key, content_type)
        return PresignUploadResponse(
            status="ready",
            doc_id=doc_id,
            filename=filename,
            upload_url=upload_url,
            storage_key=storage_key,
            expires_in=settings.B2_PRESIGN_EXPIRY,
        )

    async def confirm_upload(self, kb_id: UUID, doc_id: UUID, user_id: UUID) -> KbDocument:
        doc = await self._kb_model_service.get_doc(kb_id, doc_id, user_id)
        if doc.processing_status != KbProcessingStatusEnum.pending_upload:
            raise ConflictError("Document is not awaiting upload confirmation")

        await self._kb_model_service.mark_document_pending(doc)
        await self._kb_model_service.save_changes()

        from document_pipeline.tasks import process_document_task
        result = process_document_task.delay(str(doc_id))
        await self._kb_model_service.set_document_task(doc, result.id)
        await self._kb_model_service.save_changes()
        return doc

    async def ingest_from_s3(
        self,
        kb_id: UUID,
        user_id: UUID,
        url: str,
        filename: str,
        access_key_id: str | None,
        secret_access_key: str | None,
        region: str | None,
    ) -> KbDocument:
        await self._kb_model_service.get_kb(kb_id, user_id)

        ext = Path(filename).suffix.lower()
        if ext not in settings.KB_SUPPORTED_EXTENSIONS:
            raise ConflictError(f"Unsupported extension: {ext}")

        doc = await self._kb_model_service.create_document(
            kb_id=kb_id,
            user_id=user_id,
            filename=filename,
            source_type=KbSourceTypeEnum.s3_url,
            source_url=url,  # store for retry
            processing_status=KbProcessingStatusEnum.pending,
        )
        await self._kb_model_service.save_changes()

        creds = {}
        if access_key_id:
            creds["access_key_id"] = access_key_id
            creds["secret_access_key"] = secret_access_key or ""
            creds["region"] = region or "us-east-1"

        from document_pipeline.tasks import ingest_from_s3_task
        result = ingest_from_s3_task.delay(str(doc.id), url, creds)
        await self._kb_model_service.set_document_task(doc, result.id)
        await self._kb_model_service.save_changes()

        return doc

    async def delete_document(self, kb_id: UUID, doc_id: UUID, user_id: UUID) -> None:
        doc = await self._kb_model_service.get_doc(kb_id, doc_id, user_id)
        task_id, storage_key = await self._kb_model_service.delete_document(doc)
        await self._kb_model_service.save_changes()

        if task_id:
            try:
                from celery_app import celery_app
                celery_app.control.revoke(task_id, terminate=True)
            except Exception:
                logger.warning("Could not revoke task %s for doc %s — task may still run", task_id, doc_id)

        # B2 delete is sync boto3 — run in executor
        if storage_key:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _delete_b2_file, storage_key)

        # await directly — asyncio.create_task is fire-and-forget
        from document_pipeline import vector_store
        await vector_store.delete_document_chunks(str(kb_id), str(doc_id))

    async def retry_document(self, kb_id: UUID, doc_id: UUID, user_id: UUID) -> KbDocument:
        doc = await self._kb_model_service.get_doc(kb_id, doc_id, user_id)
        if doc.processing_status != KbProcessingStatusEnum.failed:
            raise ConflictError("Only failed documents can be retried")

        await self._kb_model_service.mark_document_for_retry(doc)
        await self._kb_model_service.save_changes()

        # dispatch correct task based on source_type
        if doc.source_type == KbSourceTypeEnum.s3_url:
            from document_pipeline.tasks import ingest_from_s3_task
            result = ingest_from_s3_task.delay(str(doc_id), doc.source_url or "", {})
        else:
            from document_pipeline.tasks import process_document_task
            result = process_document_task.delay(str(doc_id))

        await self._kb_model_service.set_document_task(doc, result.id)
        await self._kb_model_service.save_changes()
        return doc

    async def cancel_document(self, kb_id: UUID, doc_id: UUID, user_id: UUID) -> KbDocument:
        doc = await self._kb_model_service.get_doc(kb_id, doc_id, user_id)
        if doc.processing_status not in _CANCELLABLE_STATUSES:
            raise ConflictError("Document cannot be cancelled in its current state")

        doc, task_id = await self._kb_model_service.cancel_document(doc)
        await self._kb_model_service.save_changes()

        if task_id:
            from celery_app import celery_app
            celery_app.control.revoke(task_id, terminate=True)

        return doc

    async def reindex_kb(self, kb_id: UUID, user_id: UUID) -> int:
        """Re-process all ready/failed/cancelled docs. Surgical: only deletes chunks for targeted docs."""
        await self._kb_model_service.get_kb(kb_id, user_id)
        docs = await self._kb_model_service.list_docs_for_reindex(kb_id)

        to_dispatch: list[KbDocument] = []
        ready_count_delta = 0

        from document_pipeline import vector_store
        for doc in docs:
            if doc.processing_status not in _REINDEXABLE_STATUSES:
                continue
            await vector_store.delete_document_chunks(str(kb_id), str(doc.id))
            if doc.processing_status == KbProcessingStatusEnum.ready:
                ready_count_delta += 1
            doc.processing_status = KbProcessingStatusEnum.pending
            doc.error_message = None
            doc.chunk_count = 0
            doc.indexed_at = None
            to_dispatch.append(doc)

        if ready_count_delta:
            await self._kb_model_service.adjust_document_count(kb_id, ready_count_delta)

        await self._kb_model_service.save_changes()

        from document_pipeline.tasks import process_document_task, ingest_from_s3_task
        for doc in to_dispatch:
            if doc.source_type == KbSourceTypeEnum.s3_url:
                result = ingest_from_s3_task.delay(str(doc.id), doc.source_url or "", {})
            else:
                result = process_document_task.delay(str(doc.id))
            await self._kb_model_service.set_document_task(doc, result.id)
        await self._kb_model_service.save_changes()

        return len(to_dispatch)

    async def get_presigned_url(self, kb_id: UUID, doc_id: UUID, user_id: UUID) -> dict:
        doc = await self._kb_model_service.get_doc(kb_id, doc_id, user_id)
        if not doc.storage_key:
            raise NotFoundError("Document", str(doc_id))

        from document_pipeline.storage import generate_presigned_url
        url = generate_presigned_url(doc.storage_key)
        return {
            "url": url,
            "filename": doc.filename,
            "expires_in": settings.B2_PRESIGN_EXPIRY,
        }

    # -----------------------------------------------------------------------
    # Agent ↔ KB junction
    # -----------------------------------------------------------------------

    async def set_agent_kbs(self, agent_id: UUID, kb_ids: list[UUID]) -> None:
        await self._kb_model_service.set_agent_kbs(agent_id, kb_ids)

    async def get_kb_ids_for_agent(self, agent_id: UUID) -> list[UUID]:
        return await self._kb_model_service.get_kb_ids_for_agent(agent_id)


def _delete_b2_file(storage_key: str) -> None:
    try:
        from document_pipeline.storage import delete_file
        delete_file(storage_key)
    except Exception:
        logger.error("Failed to delete B2 file: %s", storage_key, exc_info=True)
