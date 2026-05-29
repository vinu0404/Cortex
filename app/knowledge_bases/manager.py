import asyncio
import hashlib
import logging
import os
import uuid
from pathlib import Path
from uuid import UUID

from sqlalchemy import and_, select, update
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

        # BUG-20: commit DB first — if external cleanup fails, KB is still gone
        await self._db.delete(kb)
        await self._db.commit()

        # BUG-14: B2 delete is sync boto3 — must run in executor to avoid blocking event loop
        loop = asyncio.get_event_loop()
        for doc in docs:
            if doc.storage_key:
                await loop.run_in_executor(None, _delete_b2_file, doc.storage_key)

        # BUG-04: await directly — asyncio.create_task is fire-and-forget, may never complete
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

    async def upload_documents(
        self,
        kb_id: UUID,
        user_id: UUID,
        files: list[tuple[str, bytes, str]],  # (filename, bytes, content_type)
    ) -> list[dict]:
        await self._get_kb(kb_id, user_id)

        if len(files) > settings.KB_MAX_FILES_PER_UPLOAD:
            raise ConflictError(
                f"Too many files. Max: {settings.KB_MAX_FILES_PER_UPLOAD}"
            )

        results = []
        for filename, file_bytes, content_type in files:
            ext = Path(filename).suffix.lower()
            if ext not in settings.KB_SUPPORTED_EXTENSIONS:
                results.append({
                    "filename": filename,
                    "status": "rejected",
                    "reason": f"Unsupported extension: {ext}",
                })
                continue

            size_mb = len(file_bytes) / (1024 * 1024)
            if size_mb > settings.KB_MAX_FILE_SIZE_MB:
                results.append({
                    "filename": filename,
                    "status": "rejected",
                    "reason": f"File too large. Max: {settings.KB_MAX_FILE_SIZE_MB} MB",
                })
                continue

            file_hash = hashlib.sha256(file_bytes).hexdigest()

            dup = await self._db.scalar(
                select(KbDocument).where(
                    and_(
                        KbDocument.kb_id == kb_id,
                        KbDocument.file_hash == file_hash,
                        KbDocument.processing_status == KbProcessingStatusEnum.ready,
                    )
                )
            )
            if dup:
                results.append({
                    "doc_id": str(dup.id),
                    "filename": filename,
                    "status": "already_indexed",
                    "skipped": True,
                })
                continue

            doc_id = uuid.uuid4()
            staging_dir = os.path.join(settings.KB_STAGING_DIR, str(doc_id))
            os.makedirs(staging_dir, exist_ok=True)
            staging_path = os.path.join(staging_dir, filename)
            with open(staging_path, "wb") as f:
                f.write(file_bytes)

            doc = KbDocument(
                id=doc_id,
                kb_id=kb_id,
                user_id=user_id,
                filename=filename,
                file_size_bytes=len(file_bytes),
                content_type=content_type,
                file_hash=file_hash,
                staging_path=staging_path,
                source_type=KbSourceTypeEnum.device,
                processing_status=KbProcessingStatusEnum.pending,
            )
            self._db.add(doc)
            # BUG-28: commit before Celery dispatch — eager workers read DB immediately
            await self._db.commit()

            from document_pipeline.tasks import process_document_task
            process_document_task.delay(str(doc_id))

            results.append({"doc_id": str(doc_id), "filename": filename, "status": "pending"})

        return results

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
            source_url=url,  # BUG-10: store for retry
            processing_status=KbProcessingStatusEnum.pending,
        )
        self._db.add(doc)
        # BUG-28: commit before Celery dispatch
        await self._db.commit()

        creds = {}
        if access_key_id:
            creds["access_key_id"] = access_key_id
            creds["secret_access_key"] = secret_access_key or ""
            creds["region"] = region or "us-east-1"

        from document_pipeline.tasks import ingest_from_s3_task
        ingest_from_s3_task.delay(str(doc.id), url, creds)

        return doc

    async def delete_document(self, kb_id: UUID, doc_id: UUID, user_id: UUID) -> None:
        doc = await self._get_doc(kb_id, doc_id, user_id)

        # BUG-21: decrement document_count atomically (only if doc was fully indexed)
        if doc.processing_status == KbProcessingStatusEnum.ready:
            await self._db.execute(
                update(KnowledgeBase)
                .where(KnowledgeBase.id == doc.kb_id)
                .values(document_count=KnowledgeBase.document_count - 1)
            )

        storage_key = doc.storage_key
        # BUG-20: commit DB first — if external cleanup fails, doc is still gone
        await self._db.delete(doc)
        await self._db.commit()

        # BUG-14: B2 delete is sync boto3 — run in executor
        if storage_key:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _delete_b2_file, storage_key)

        # BUG-04: await directly — asyncio.create_task is fire-and-forget
        from document_pipeline import vector_store
        await vector_store.delete_document_chunks(str(kb_id), str(doc_id))

    async def retry_document(self, kb_id: UUID, doc_id: UUID, user_id: UUID) -> KbDocument:
        doc = await self._get_doc(kb_id, doc_id, user_id)
        if doc.processing_status != KbProcessingStatusEnum.failed:
            raise ConflictError("Only failed documents can be retried")

        doc.processing_status = KbProcessingStatusEnum.pending
        doc.error_message = None
        # BUG-28: commit before dispatch
        await self._db.commit()

        # BUG-09: dispatch correct task based on source_type
        if doc.source_type == KbSourceTypeEnum.s3_url:
            from document_pipeline.tasks import ingest_from_s3_task
            ingest_from_s3_task.delay(str(doc_id), doc.source_url or "", {})
        else:
            from document_pipeline.tasks import process_document_task
            process_document_task.delay(str(doc_id))
        return doc

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
