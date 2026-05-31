import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.session import Base


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        Index(
            "uq_workspaces_user_name_active",
            "user_id", "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    embed_token: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    embed_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    embed_hitl_auto_approve: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    embed_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    embed_budget_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    embed_spend_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    embed_spend_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    agents: Mapped[list] = relationship(
        "Agent", back_populates="workspace", cascade="all, delete-orphan"
    )
