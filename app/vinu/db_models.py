import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
