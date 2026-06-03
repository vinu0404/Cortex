import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.session import Base


class Persona(Base):
    __tablename__ = "personas"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    agent_personas: Mapped[list["AgentPersona"]] = relationship(
        "AgentPersona", back_populates="persona", cascade="all, delete-orphan"
    )


class AgentPersona(Base):
    __tablename__ = "agent_personas"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )
    persona_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("personas.id", ondelete="CASCADE"), primary_key=True
    )

    agent: Mapped["Agent"] = relationship("Agent", back_populates="agent_personas")
    persona: Mapped["Persona"] = relationship("Persona", back_populates="agent_personas")


class PersonaModelService:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def list_personas(self, user_id: uuid.UUID) -> list[Persona]:
        result = await self._db.scalars(
            select(Persona).where(Persona.user_id == user_id).order_by(Persona.created_at.desc())
        )
        return list(result)

    async def create_persona(
        self,
        *,
        user_id: uuid.UUID,
        name: str,
        description: str | None,
        system_prompt: str,
    ) -> Persona:
        persona = Persona(user_id=user_id, name=name, description=description, system_prompt=system_prompt)
        self._db.add(persona)
        await self._db.flush()
        return persona

    async def get_persona(self, persona_id: uuid.UUID) -> Persona | None:
        return await self._db.get(Persona, persona_id)

    async def update_persona_fields(self, persona: Persona, **kwargs) -> Persona:
        for field, value in kwargs.items():
            if value is not None:
                setattr(persona, field, value)
        persona.updated_at = datetime.now(timezone.utc)
        return persona

    async def delete_persona(self, persona: Persona) -> None:
        await self._db.delete(persona)
