import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from database.session import Base


class UserApiKey(Base):
    __tablename__ = "user_api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key_name: Mapped[str] = mapped_column(String, nullable=False)
    encrypted_key: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str | None] = mapped_column(String, nullable=True)
    available_models: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class ApiKeyModelService:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def create_key(
        self,
        *,
        user_id: uuid.UUID,
        key_name: str,
        encrypted_key: str,
        provider: str,
        available_models: list,
    ) -> UserApiKey:
        key_record = UserApiKey(
            user_id=user_id,
            key_name=key_name,
            encrypted_key=encrypted_key,
            provider=provider,
            available_models=available_models,
        )
        self._db.add(key_record)
        await self._db.flush()
        return key_record

    async def list_keys(self, user_id: uuid.UUID) -> list[UserApiKey]:
        result = await self._db.scalars(
            select(UserApiKey).where(UserApiKey.user_id == user_id).order_by(UserApiKey.created_at.desc())
        )
        return list(result)

    async def get_key(self, key_id: uuid.UUID) -> UserApiKey | None:
        return await self._db.get(UserApiKey, key_id)

    async def update_models(self, key_record: UserApiKey, models: list[str]) -> list[str]:
        key_record.available_models = models
        await self._db.flush()
        return models

    async def delete_key(self, key_record: UserApiKey) -> None:
        await self._db.delete(key_record)
