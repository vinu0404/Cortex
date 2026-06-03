import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, UniqueConstraint, delete, select
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.common.exceptions import ConflictError, NotFoundError

from database.session import Base


class AuthTypeEnum(str, enum.Enum):
    oauth2 = "oauth2"
    apikey = "apikey"
    credentials = "credentials"
    internal = "internal"


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


class ConnectorModelService:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def get_definition(self, slug: str) -> ConnectorDefinition:
        defn = await self._db.scalar(
            select(ConnectorDefinition).where(ConnectorDefinition.slug == slug)
        )
        if not defn:
            raise NotFoundError("ConnectorDefinition", slug)
        return defn

    async def seed_definition(self, definition: dict) -> None:
        existing = await self._db.scalar(
            select(ConnectorDefinition).where(ConnectorDefinition.slug == definition["slug"])
        )
        if not existing:
            self._db.add(ConnectorDefinition(**definition))

    async def flush_seed(self) -> None:
        await self._db.flush()

    async def list_definitions(self) -> list[ConnectorDefinition]:
        return list(
            await self._db.scalars(
                select(ConnectorDefinition).where(ConnectorDefinition.is_active.is_(True))
            )
        )

    async def list_user_instances(self, user_id: uuid.UUID) -> list[ConnectorInstance]:
        from sqlalchemy.orm import selectinload

        return list(
            await self._db.scalars(
                select(ConnectorInstance)
                .where(ConnectorInstance.user_id == user_id)
                .where(ConnectorInstance.status == ConnectorStatusEnum.active)
                .options(selectinload(ConnectorInstance.definition))
            )
        )

    async def create_instance(
        self,
        *,
        user_id: uuid.UUID,
        definition_id: uuid.UUID,
        encrypted_tokens: str,
        account_label: str | None,
        status: ConnectorStatusEnum,
        token_expires_at: datetime | None,
    ) -> ConnectorInstance:
        instance = ConnectorInstance(
            user_id=user_id,
            definition_id=definition_id,
            encrypted_tokens=encrypted_tokens,
            account_label=account_label,
            status=status,
            token_expires_at=token_expires_at,
        )
        self._db.add(instance)
        try:
            await self._db.flush()
        except IntegrityError as e:
            raise ConflictError("Connector already connected") from e
        return instance

    async def replace_credentials_instance(
        self,
        *,
        user_id: uuid.UUID,
        definition_id: uuid.UUID,
        encrypted_tokens: str,
        account_label: str | None,
    ) -> ConnectorInstance:
        await self._db.execute(
            delete(ConnectorInstance).where(
                ConnectorInstance.user_id == user_id,
                ConnectorInstance.definition_id == definition_id,
            )
        )
        instance = ConnectorInstance(
            user_id=user_id,
            definition_id=definition_id,
            encrypted_tokens=encrypted_tokens,
            account_label=account_label,
            status=ConnectorStatusEnum.active,
        )
        self._db.add(instance)
        await self._db.flush()
        await self._db.commit()
        await self._db.refresh(instance)
        return instance

    async def get_instance_for_user(
        self,
        instance_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> ConnectorInstance:
        instance = await self._db.get(ConnectorInstance, instance_id)
        if not instance or instance.user_id != user_id:
            raise NotFoundError("ConnectorInstance", str(instance_id))
        return instance

    async def delete_instance(self, instance: ConnectorInstance) -> None:
        await self._db.delete(instance)

    async def update_instance_tokens(
        self,
        instance: ConnectorInstance,
        encrypted_tokens: str,
        token_expires_at: datetime | None,
    ) -> None:
        instance.encrypted_tokens = encrypted_tokens
        instance.token_expires_at = token_expires_at
        instance.updated_at = datetime.now(timezone.utc)
        await self._db.flush()

    async def save_changes(self) -> None:
        await self._db.commit()

    async def refresh(self, instance) -> None:
        await self._db.refresh(instance)
