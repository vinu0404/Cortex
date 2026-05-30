import asyncio
import logging
import uuid
from pathlib import Path
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.knowledge_bases.db_models import (
    AgentKnowledgeBase,
    KbDocument,
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

    # -----------------------------------------------------------------------
    # Knowledge Bases
    # -----------------------------------------------------------------------

    async def list_kbs(self, user_id: UUID) -> list[KnowledgeBase]:
        result = await self._db.scalars(
            select(KnowledgeBase)
            .where(KnowledgeBase.user_id == user_id)
            .order_by(KnowledgeBase.created_at.desc())
        )
        return list(result)

    async def create_kb(self, user_id: UUID, name: str, description: str | None) -> KnowledgeBase:
        kb = KnowledgeBase(user_id=user_id, name=name, description=description)
        self._db.add(kb)
        await self._db.flush()
        return kb

    async def delete_kb(self, kb_id: UUID, user_id: UUID) -> None:
        kb = await self._get_kb(kb_id, user_id)
        docs_result = await self._db.scalars(select(KbDocument).where(KbDocument.kb_id == kb_id))
        docs = list(docs_result)

        # commit DB first — if external cleanup fails, KB is still gone
        await self._db.delete(kb)
        await self._db.commit()

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
        await self._get_kb(kb_id, user_id)
        result = await self._db.scalars(
            select(KbDocument)
            .where(KbDocument.kb_id == kb_id)
            .order_by(KbDocument.created_at.desc())
        )
        return list(result)

    async def presign_upload(
        self,
        kb_id: UUID,
        user_id: UUID,
        filename: str,
        content_type: str,
        file_size_bytes: int,
    ) -> dict:
        await self._get_kb(kb_id, user_id)

        ext = Path(filename).suffix.lower()
        if ext not in settings.KB_SUPPORTED_EXTENSIONS:
            raise ConflictError(f"Unsupported extension: {ext}")
        if file_size_bytes > settings.KB_MAX_FILE_SIZE_MB * 1024 * 1024:
            raise ConflictError(f"File too large. Max: {settings.KB_MAX_FILE_SIZE_MB} MB")

        doc_id = uuid.uuid4()
        from document_pipeline.storage import build_kb_storage_key, generate_presigned_put_url
        storage_key = build_kb_storage_key(str(kb_id), str(doc_id), filename)

        doc = KbDocument(
            id=doc_id,
            kb_id=kb_id,
            user_id=user_id,
            filename=filename,
            file_size_bytes=file_size_bytes,
            content_type=content_type,
            storage_key=storage_key,
            staging_path=None,
            source_type=KbSourceTypeEnum.device,
            processing_status=KbProcessingStatusEnum.pending_upload,
        )
        self._db.add(doc)
        await self._db.commit()

        upload_url = generate_presigned_put_url(storage_key, content_type)
        return {
            "doc_id": str(doc_id),
            "upload_url": upload_url,
            "storage_key": storage_key,
            "expires_in": settings.B2_PRESIGN_EXPIRY,
        }

    async def confirm_upload(self, kb_id: UUID, doc_id: UUID, user_id: UUID) -> KbDocument:
        doc = await self._get_doc(kb_id, doc_id, user_id)
        if doc.processing_status != KbProcessingStatusEnum.pending_upload:
            raise ConflictError("Document is not awaiting upload confirmation")

        doc.processing_status = KbProcessingStatusEnum.pending
        await self._db.commit()

        from document_pipeline.tasks import process_document_task
        result = process_document_task.delay(str(doc_id))
        doc.celery_task_id = result.id
        await self._db.commit()
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
        await self._get_kb(kb_id, user_id)

        ext = Path(filename).suffix.lower()
        if ext not in settings.KB_SUPPORTED_EXTENSIONS:
            raise ConflictError(f"Unsupported extension: {ext}")

        doc = KbDocument(
            kb_id=kb_id,
            user_id=user_id,
            filename=filename,
            source_type=KbSourceTypeEnum.s3_url,
            source_url=url,  # store for retry
            processing_status=KbProcessingStatusEnum.pending,
        )
        self._db.add(doc)
        # commit before Celery dispatch
        await self._db.commit()

        creds = {}
        if access_key_id:
            creds["access_key_id"] = access_key_id
            creds["secret_access_key"] = secret_access_key or ""
            creds["region"] = region or "us-east-1"

        from document_pipeline.tasks import ingest_from_s3_task
        result = ingest_from_s3_task.delay(str(doc.id), url, creds)
        doc.celery_task_id = result.id
        await self._db.commit()

        return doc

    async def delete_document(self, kb_id: UUID, doc_id: UUID, user_id: UUID) -> None:
        doc = await self._get_doc(kb_id, doc_id, user_id)

        # decrement document_count atomically (only if doc was fully indexed)
        if doc.processing_status == KbProcessingStatusEnum.ready:
            await self._db.execute(
                update(KnowledgeBase)
                .where(KnowledgeBase.id == doc.kb_id)
                .values(document_count=KnowledgeBase.document_count - 1)
            )

        task_id = doc.celery_task_id
        storage_key = doc.storage_key
        # commit DB first — if external cleanup fails, doc is still gone
        await self._db.delete(doc)
        await self._db.commit()

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
        doc = await self._get_doc(kb_id, doc_id, user_id)
        if doc.processing_status != KbProcessingStatusEnum.failed:
            raise ConflictError("Only failed documents can be retried")

        doc.processing_status = KbProcessingStatusEnum.pending
        doc.error_message = None
        # commit before dispatch
        await self._db.commit()

        # dispatch correct task based on source_type
        if doc.source_type == KbSourceTypeEnum.s3_url:
            from document_pipeline.tasks import ingest_from_s3_task
            result = ingest_from_s3_task.delay(str(doc_id), doc.source_url or "", {})
        else:
            from document_pipeline.tasks import process_document_task
            result = process_document_task.delay(str(doc_id))

        doc.celery_task_id = result.id
        await self._db.commit()
        return doc

    async def cancel_document(self, kb_id: UUID, doc_id: UUID, user_id: UUID) -> KbDocument:
        doc = await self._get_doc(kb_id, doc_id, user_id)
        if doc.processing_status not in _CANCELLABLE_STATUSES:
            raise ConflictError("Document cannot be cancelled in its current state")

        task_id = doc.celery_task_id
        doc.processing_status = KbProcessingStatusEnum.cancelled
        doc.celery_task_id = None
        await self._db.commit()

        if task_id:
            from celery_app import celery_app
            celery_app.control.revoke(task_id, terminate=True)

        return doc

    async def reindex_kb(self, kb_id: UUID, user_id: UUID) -> int:
        """Re-process all ready/failed/cancelled docs. Surgical: only deletes chunks for targeted docs."""
        await self._get_kb(kb_id, user_id)
        docs_result = await self._db.scalars(select(KbDocument).where(KbDocument.kb_id == kb_id))
        docs = list(docs_result)

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
            await self._db.execute(
                update(KnowledgeBase)
                .where(KnowledgeBase.id == kb_id)
                .values(document_count=KnowledgeBase.document_count - ready_count_delta)
            )

        await self._db.commit()

        from document_pipeline.tasks import process_document_task, ingest_from_s3_task
        for doc in to_dispatch:
            if doc.source_type == KbSourceTypeEnum.s3_url:
                result = ingest_from_s3_task.delay(str(doc.id), doc.source_url or "", {})
            else:
                result = process_document_task.delay(str(doc.id))
            doc.celery_task_id = result.id
        await self._db.commit()

        return len(to_dispatch)

    async def get_presigned_url(self, kb_id: UUID, doc_id: UUID, user_id: UUID) -> dict:
        doc = await self._get_doc(kb_id, doc_id, user_id)
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
        result = await self._db.scalars(
            select(AgentKnowledgeBase).where(AgentKnowledgeBase.agent_id == agent_id)
        )
        for existing in result:
            await self._db.delete(existing)
        for kb_id in kb_ids:
            self._db.add(AgentKnowledgeBase(agent_id=agent_id, kb_id=kb_id))
        await self._db.flush()

    async def get_kb_ids_for_agent(self, agent_id: UUID) -> list[UUID]:
        result = await self._db.scalars(
            select(AgentKnowledgeBase).where(AgentKnowledgeBase.agent_id == agent_id)
        )
        return [row.kb_id for row in result]

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    async def _get_kb(self, kb_id: UUID, user_id: UUID) -> KnowledgeBase:
        kb = await self._db.get(KnowledgeBase, kb_id)
        if not kb:
            raise NotFoundError("KnowledgeBase", str(kb_id))
        if kb.user_id != user_id:
            raise ForbiddenError("Access denied")
        return kb

    async def _get_doc(self, kb_id: UUID, doc_id: UUID, user_id: UUID) -> KbDocument:
        doc = await self._db.get(KbDocument, doc_id)
        if not doc or doc.kb_id != kb_id:
            raise NotFoundError("KbDocument", str(doc_id))
        if doc.user_id != user_id:
            raise ForbiddenError("Access denied")
        return doc


def _delete_b2_file(storage_key: str) -> None:
    try:
        from document_pipeline.storage import delete_file
        delete_file(storage_key)
    except Exception:
        logger.error("Failed to delete B2 file: %s", storage_key, exc_info=True)
