import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, PrimaryKeyConstraint, String, Text, UniqueConstraint, select, update
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.common.exceptions import ForbiddenError, NotFoundError

from database.session import Base


class WcCrawlStatusEnum(str, enum.Enum):
    pending = "pending"
    crawling = "crawling"
    processing = "processing"
    ready = "ready"
    failed = "failed"
    cancelled = "cancelled"


class WebsiteCollection(Base):
    __tablename__ = "website_collections"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_website_collections_user_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    url_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    urls: Mapped[list["WebsiteUrl"]] = relationship(
        "WebsiteUrl", back_populates="collection", cascade="all, delete-orphan"
    )


class WebsiteUrl(Base):
    __tablename__ = "website_urls"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("website_collections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    max_depth: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    crawl_status: Mapped[WcCrawlStatusEnum] = mapped_column(
        Enum(WcCrawlStatusEnum, create_type=False), default=WcCrawlStatusEnum.pending, nullable=False, index=True
    )
    page_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    login_blocked_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    celery_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    collection: Mapped["WebsiteCollection"] = relationship("WebsiteCollection", back_populates="urls")


class AgentWebsiteCollection(Base):
    __tablename__ = "agent_website_collections"
    __table_args__ = (PrimaryKeyConstraint("agent_id", "collection_id"),)

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("website_collections.id", ondelete="CASCADE"), nullable=False
    )


class WebsiteCollectionModelService:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def list_collections(self, user_id: uuid.UUID) -> list[WebsiteCollection]:
        return list(
            await self._db.scalars(
                select(WebsiteCollection)
                .where(WebsiteCollection.user_id == user_id)
                .order_by(WebsiteCollection.created_at.desc())
            )
        )

    async def create_collection(self, user_id: uuid.UUID, name: str, description: str | None) -> WebsiteCollection:
        wc = WebsiteCollection(user_id=user_id, name=name, description=description)
        self._db.add(wc)
        await self._db.flush()
        return wc

    async def get_collection(self, collection_id: uuid.UUID, user_id: uuid.UUID) -> WebsiteCollection:
        wc = await self._db.get(WebsiteCollection, collection_id)
        if not wc:
            raise NotFoundError("WebsiteCollection", str(collection_id))
        if wc.user_id != user_id:
            raise ForbiddenError("Access denied")
        return wc

    async def delete_collection(self, wc: WebsiteCollection) -> None:
        await self._db.delete(wc)

    async def list_urls(self, collection_id: uuid.UUID) -> list[WebsiteUrl]:
        return list(
            await self._db.scalars(
                select(WebsiteUrl)
                .where(WebsiteUrl.collection_id == collection_id)
                .order_by(WebsiteUrl.created_at.asc())
            )
        )

    async def create_url(
        self,
        collection_id: uuid.UUID,
        user_id: uuid.UUID,
        url: str,
        max_depth: int,
    ) -> WebsiteUrl:
        wu = WebsiteUrl(
            collection_id=collection_id,
            user_id=user_id,
            url=url,
            max_depth=max_depth,
        )
        self._db.add(wu)
        await self._db.flush()
        await self._db.execute(
            update(WebsiteCollection)
            .where(WebsiteCollection.id == collection_id)
            .values(url_count=WebsiteCollection.url_count + 1)
        )
        return wu

    async def get_url(self, collection_id: uuid.UUID, url_id: uuid.UUID, user_id: uuid.UUID) -> WebsiteUrl:
        wu = await self._db.get(WebsiteUrl, url_id)
        if not wu or wu.collection_id != collection_id:
            raise NotFoundError("WebsiteUrl", str(url_id))
        if wu.user_id != user_id:
            raise ForbiddenError("Access denied")
        return wu

    async def delete_url(self, collection_id: uuid.UUID, wu: WebsiteUrl) -> str | None:
        task_id = wu.celery_task_id
        await self._db.delete(wu)
        await self._db.execute(
            update(WebsiteCollection)
            .where(WebsiteCollection.id == collection_id)
            .values(url_count=WebsiteCollection.url_count - 1)
        )
        return task_id

    async def set_url_pending(self, wu: WebsiteUrl) -> WebsiteUrl:
        wu.crawl_status = WcCrawlStatusEnum.pending
        wu.error_message = None
        return wu

    async def set_url_task(self, wu: WebsiteUrl, task_id: str) -> WebsiteUrl:
        wu.celery_task_id = task_id
        return wu

    async def cancel_url(self, wu: WebsiteUrl) -> tuple[WebsiteUrl, str | None]:
        task_id = wu.celery_task_id
        wu.crawl_status = WcCrawlStatusEnum.cancelled
        wu.celery_task_id = None
        return wu, task_id

    async def set_agent_collections(self, agent_id: uuid.UUID, collection_ids: list[uuid.UUID]) -> None:
        result = await self._db.scalars(
            select(AgentWebsiteCollection).where(AgentWebsiteCollection.agent_id == agent_id)
        )
        for existing in result:
            await self._db.delete(existing)
        for collection_id in collection_ids:
            self._db.add(AgentWebsiteCollection(agent_id=agent_id, collection_id=collection_id))
        await self._db.flush()

    async def get_collection_ids_for_agent(self, agent_id: uuid.UUID) -> list[uuid.UUID]:
        result = await self._db.scalars(
            select(AgentWebsiteCollection).where(AgentWebsiteCollection.agent_id == agent_id)
        )
        return [row.collection_id for row in result]

    async def get_agent_collection_links(self, agent_ids: list[uuid.UUID]) -> list[AgentWebsiteCollection]:
        return list(
            await self._db.scalars(
                select(AgentWebsiteCollection).where(AgentWebsiteCollection.agent_id.in_(agent_ids))
            )
        )

    async def save_changes(self) -> None:
        await self._db.commit()

    async def undo_changes(self) -> None:
        await self._db.rollback()
