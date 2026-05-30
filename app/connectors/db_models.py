import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.session import Base


class AuthTypeEnum(str, enum.Enum):
    oauth2 = "oauth2"
    apikey = "apikey"
    credentials = "credentials"


class ConnectorStatusEnum(str, enum.Enum):
    active = "active"
    expired = "expired"
    revoked = "revoked"


class ConnectorDefinition(Base):
    __tablename__ = "connector_definitions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    auth_type: Mapped[AuthTypeEnum] = mapped_column(Enum(AuthTypeEnum, create_type=False), nullable=False)
    tools: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    icon: Mapped[str | None] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    instances: Mapped[list["ConnectorInstance"]] = relationship(
        "ConnectorInstance", back_populates="definition"
    )


class ConnectorInstance(Base):
    __tablename__ = "connector_instances"
    __table_args__ = (
        UniqueConstraint("user_id", "definition_id", name="uq_connector_user_definition"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("connector_definitions.id", ondelete="CASCADE"),
        nullable=False,
    )
    encrypted_tokens: Mapped[str] = mapped_column(Text, nullable=False)
    account_label: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[ConnectorStatusEnum] = mapped_column(
        Enum(ConnectorStatusEnum, create_type=False), default=ConnectorStatusEnum.active, nullable=False
    )
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    definition: Mapped["ConnectorDefinition"] = relationship(
        "ConnectorDefinition", back_populates="instances"
    )
