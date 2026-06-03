import enum
import uuid
from datetime import datetime, timezone
from uuid import UUID as PyUUID

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.auth.db_models import User
from app.common.exceptions import ForbiddenError, NotFoundError

from database.session import Base


class VinuMessageRoleEnum(str, enum.Enum):
    user = "user"
    assistant = "assistant"


class VinuConversation(Base):
    __tablename__ = "vinu_conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False, default="New Chat")
    last_plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_build: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    messages: Mapped[list["VinuMessage"]] = relationship(
        "VinuMessage", back_populates="conversation", cascade="all, delete-orphan"
    )
    summaries: Mapped[list["VinuSummary"]] = relationship(
        "VinuSummary", back_populates="conversation", cascade="all, delete-orphan"
    )


class VinuMessage(Base):
    __tablename__ = "vinu_messages"
    __table_args__ = (Index("ix_vinu_messages_conv_created", "conversation_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vinu_conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[VinuMessageRoleEnum] = mapped_column(
        Enum(VinuMessageRoleEnum, native_enum=False), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    conversation: Mapped["VinuConversation"] = relationship(back_populates="messages")


class VinuSummary(Base):
    __tablename__ = "vinu_summaries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vinu_conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    message_range_start: Mapped[int] = mapped_column(Integer, nullable=False)
    message_range_end: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    conversation: Mapped["VinuConversation"] = relationship(back_populates="summaries")


class VinuModelService:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def list_conversations(
        self,
        user_id: PyUUID,
        limit: int,
        cursor_created_at: datetime | None,
        cursor_id: PyUUID | None,
    ) -> list[VinuConversation]:
        query = (
            select(VinuConversation)
            .where(VinuConversation.user_id == user_id)
            .order_by(VinuConversation.created_at.desc(), VinuConversation.id.desc())
            .limit(limit + 1)
        )
        if cursor_created_at and cursor_id:
            query = query.where(
                (VinuConversation.created_at < cursor_created_at)
                | ((VinuConversation.created_at == cursor_created_at) & (VinuConversation.id < cursor_id))
            )
        return list(await self._db.scalars(query))

    async def create_conversation(self, user_id: PyUUID, name: str = "New Chat") -> VinuConversation:
        conv = VinuConversation(user_id=user_id, name=name)
        self._db.add(conv)
        await self._db.flush()
        return conv

    async def get_conversation(self, conv_id: PyUUID, user_id: PyUUID) -> VinuConversation:
        conv = await self._db.get(VinuConversation, conv_id)
        if not conv:
            raise NotFoundError("VinuConversation", str(conv_id))
        if conv.user_id != user_id:
            raise ForbiddenError("Access denied")
        return conv

    async def delete_conversation(self, conv_id: PyUUID, user_id: PyUUID) -> None:
        conv = await self.get_conversation(conv_id, user_id)
        await self._db.delete(conv)

    async def load_messages(self, conv_id: PyUUID) -> list[dict]:
        summary_row = await self._db.scalar(
            select(VinuSummary)
            .where(VinuSummary.conversation_id == conv_id)
            .order_by(VinuSummary.created_at.desc())
            .limit(1)
        )
        rows = list(await self._db.scalars(
            select(VinuMessage)
            .where(VinuMessage.conversation_id == conv_id)
            .order_by(VinuMessage.created_at.asc())
        ))
        messages = [{"role": row.role.value, "content": row.content} for row in rows]
        if summary_row:
            return [{"role": "system", "content": f"[Summary of earlier conversation] {summary_row.summary}"}] + messages
        return messages

    async def list_messages_paginated(
        self,
        conv_id: PyUUID,
        limit: int,
        cursor_created_at: datetime | None,
        cursor_id: PyUUID | None,
    ) -> list[VinuMessage]:
        query = (
            select(VinuMessage)
            .where(VinuMessage.conversation_id == conv_id)
            .order_by(VinuMessage.created_at.desc(), VinuMessage.id.desc())
            .limit(limit + 1)
        )
        if cursor_created_at and cursor_id:
            query = query.where(
                (VinuMessage.created_at < cursor_created_at)
                | ((VinuMessage.created_at == cursor_created_at) & (VinuMessage.id < cursor_id))
            )
        return list(await self._db.scalars(query))

    async def append_messages(self, conv_id: PyUUID, messages: list[dict]) -> None:
        for message in messages:
            self._db.add(
                VinuMessage(
                    conversation_id=conv_id,
                    role=VinuMessageRoleEnum(message["role"]),
                    content=message["content"],
                )
            )
        await self._db.execute(
            update(VinuConversation)
            .where(VinuConversation.id == conv_id)
            .values(updated_at=datetime.now(timezone.utc))
        )
        await self._db.flush()

    async def save_summary(self, conv_id: PyUUID, summary: str, range_start: int, range_end: int) -> None:
        await self._db.execute(delete(VinuSummary).where(VinuSummary.conversation_id == conv_id))
        self._db.add(
            VinuSummary(
                conversation_id=conv_id,
                summary=summary,
                message_range_start=range_start,
                message_range_end=range_end,
            )
        )
        await self._db.flush()

    async def update_agent_name(self, user_id: PyUUID, name: str | None) -> None:
        await self._db.execute(
            update(User).where(User.id == user_id).values(vinu_agent_name=name)
        )
        await self._db.flush()

    async def update_name(self, conv_id: PyUUID, name: str) -> None:
        await self._db.execute(
            update(VinuConversation).where(VinuConversation.id == conv_id).values(name=name)
        )
        await self._db.flush()

    async def save_plan(self, conv_id: PyUUID, plan: dict) -> None:
        await self._db.execute(
            update(VinuConversation).where(VinuConversation.id == conv_id).values(last_plan=plan)
        )
        await self._db.flush()

    async def save_build(self, conv_id: PyUUID, build_result: dict) -> None:
        await self._db.execute(
            update(VinuConversation).where(VinuConversation.id == conv_id).values(last_build=build_result)
        )
        await self._db.flush()

    async def list_active_mcp_servers(self, user_id: PyUUID):
        from app.mcp_servers.db_models import MCPServer

        return list(
            await self._db.scalars(
                select(MCPServer).where(MCPServer.user_id == user_id, MCPServer.is_active.is_(True))
            )
        )
