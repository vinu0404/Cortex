import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, PrimaryKeyConstraint, String, Text, UniqueConstraint, select, update
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.common.exceptions import ForbiddenError, NotFoundError

from database.session import Base


class KbSourceTypeEnum(str, enum.Enum):
    device = "device"
    s3_url = "s3_url"
    gdrive = "gdrive"


class KbProcessingStatusEnum(str, enum.Enum):
    pending_upload = "pending_upload"
    pending = "pending"
    uploading = "uploading"
    processing = "processing"
    ready = "ready"
    failed = "failed"
    cancelled = "cancelled"


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_knowledge_bases_user_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    documents: Mapped[list["KbDocument"]] = relationship(
        "KbDocument", back_populates="knowledge_base", cascade="all, delete-orphan"
    )


class KbDocument(Base):
    __tablename__ = "kb_documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String, nullable=False)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String, nullable=True)
    file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    storage_key: Mapped[str | None] = mapped_column(String, nullable=True)
    staging_path: Mapped[str | None] = mapped_column(String, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[KbSourceTypeEnum] = mapped_column(
        Enum(KbSourceTypeEnum, create_type=False), default=KbSourceTypeEnum.device, nullable=False
    )
    processing_status: Mapped[KbProcessingStatusEnum] = mapped_column(
        Enum(KbProcessingStatusEnum, create_type=False), default=KbProcessingStatusEnum.pending, nullable=False, index=True
    )
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    embedding_model: Mapped[str | None] = mapped_column(String, nullable=True)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    celery_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    knowledge_base: Mapped["KnowledgeBase"] = relationship("KnowledgeBase", back_populates="documents")


class AgentKnowledgeBase(Base):
    __tablename__ = "agent_knowledge_bases"
    __table_args__ = (PrimaryKeyConstraint("agent_id", "kb_id"),)

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )


class KnowledgeBaseModelService:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def list_kbs(self, user_id: uuid.UUID) -> list[KnowledgeBase]:
        return list(
            await self._db.scalars(
                select(KnowledgeBase)
                .where(KnowledgeBase.user_id == user_id)
                .order_by(KnowledgeBase.created_at.desc())
            )
        )

    async def create_kb(self, user_id: uuid.UUID, name: str, description: str | None) -> KnowledgeBase:
        kb = KnowledgeBase(user_id=user_id, name=name, description=description)
        self._db.add(kb)
        await self._db.flush()
        return kb

    async def get_kb(self, kb_id: uuid.UUID, user_id: uuid.UUID) -> KnowledgeBase:
        kb = await self._db.get(KnowledgeBase, kb_id)
        if not kb:
            raise NotFoundError("KnowledgeBase", str(kb_id))
        if kb.user_id != user_id:
            raise ForbiddenError("Access denied")
        return kb

    async def list_documents(self, kb_id: uuid.UUID) -> list[KbDocument]:
        return list(
            await self._db.scalars(
                select(KbDocument).where(KbDocument.kb_id == kb_id).order_by(KbDocument.created_at.desc())
            )
        )

    async def get_doc(self, kb_id: uuid.UUID, doc_id: uuid.UUID, user_id: uuid.UUID) -> KbDocument:
        doc = await self._db.get(KbDocument, doc_id)
        if not doc or doc.kb_id != kb_id:
            raise NotFoundError("KbDocument", str(doc_id))
        if doc.user_id != user_id:
            raise ForbiddenError("Access denied")
        return doc

    async def find_doc_by_hash(self, kb_id: uuid.UUID, file_hash: str) -> KbDocument | None:
        return await self._db.scalar(
            select(KbDocument)
            .where(KbDocument.kb_id == kb_id, KbDocument.file_hash == file_hash)
            .order_by(KbDocument.created_at.desc())
            .limit(1)
        )

    async def create_document(self, **kwargs) -> KbDocument:
        doc = KbDocument(**kwargs)
        self._db.add(doc)
        await self._db.flush()
        return doc

    async def mark_document_pending(self, doc: KbDocument) -> KbDocument:
        doc.processing_status = KbProcessingStatusEnum.pending
        return doc

    async def set_document_task(self, doc: KbDocument, task_id: str) -> KbDocument:
        doc.celery_task_id = task_id
        return doc

    async def delete_kb(self, kb: KnowledgeBase) -> list[KbDocument]:
        docs = list(await self._db.scalars(select(KbDocument).where(KbDocument.kb_id == kb.id)))
        await self._db.delete(kb)
        return docs

    async def delete_document(self, doc: KbDocument) -> tuple[str | None, str | None]:
        if doc.processing_status == KbProcessingStatusEnum.ready:
            await self._db.execute(
                update(KnowledgeBase)
                .where(KnowledgeBase.id == doc.kb_id)
                .values(document_count=KnowledgeBase.document_count - 1)
            )
        task_id = doc.celery_task_id
        storage_key = doc.storage_key
        await self._db.delete(doc)
        return task_id, storage_key

    async def mark_document_for_retry(self, doc: KbDocument) -> KbDocument:
        doc.processing_status = KbProcessingStatusEnum.pending
        doc.error_message = None
        return doc

    async def cancel_document(self, doc: KbDocument) -> tuple[KbDocument, str | None]:
        task_id = doc.celery_task_id
        doc.processing_status = KbProcessingStatusEnum.cancelled
        doc.celery_task_id = None
        return doc, task_id

    async def list_docs_for_reindex(self, kb_id: uuid.UUID) -> list[KbDocument]:
        return list(await self._db.scalars(select(KbDocument).where(KbDocument.kb_id == kb_id)))

    async def adjust_document_count(self, kb_id: uuid.UUID, ready_count_delta: int) -> None:
        await self._db.execute(
            update(KnowledgeBase)
            .where(KnowledgeBase.id == kb_id)
            .values(document_count=KnowledgeBase.document_count - ready_count_delta)
        )

    async def set_agent_kbs(self, agent_id: uuid.UUID, kb_ids: list[uuid.UUID]) -> None:
        result = await self._db.scalars(
            select(AgentKnowledgeBase).where(AgentKnowledgeBase.agent_id == agent_id)
        )
        for existing in result:
            await self._db.delete(existing)
        for kb_id in kb_ids:
            self._db.add(AgentKnowledgeBase(agent_id=agent_id, kb_id=kb_id))
        await self._db.flush()

    async def get_kb_ids_for_agent(self, agent_id: uuid.UUID) -> list[uuid.UUID]:
        result = await self._db.scalars(
            select(AgentKnowledgeBase).where(AgentKnowledgeBase.agent_id == agent_id)
        )
        return [row.kb_id for row in result]

    async def save_changes(self) -> None:
        await self._db.commit()

    async def undo_changes(self) -> None:
        await self._db.rollback()
