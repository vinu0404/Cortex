import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import ForbiddenError, NotFoundError
from app.personas.db_models import Persona

logger = logging.getLogger(__name__)


class PersonaManager:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def list_personas(self, user_id: UUID) -> list[Persona]:
        result = await self._db.scalars(
            select(Persona).where(Persona.user_id == user_id).order_by(Persona.created_at.desc())
        )
        return list(result)

    async def create_persona(self, user_id: UUID, name: str, description: str | None, system_prompt: str) -> Persona:
        persona = Persona(user_id=user_id, name=name, description=description, system_prompt=system_prompt)
        self._db.add(persona)
        await self._db.flush()
        return persona

    async def update_persona(self, persona_id: UUID, user_id: UUID, **kwargs) -> Persona:
        persona = await self._get_owned(persona_id, user_id)
        for field, value in kwargs.items():
            if value is not None:
                setattr(persona, field, value)
        persona.updated_at = datetime.now(timezone.utc)
        return persona

    async def delete_persona(self, persona_id: UUID, user_id: UUID) -> None:
        persona = await self._get_owned(persona_id, user_id)
        await self._db.delete(persona)

    async def _get_owned(self, persona_id: UUID, user_id: UUID) -> Persona:
        persona = await self._db.get(Persona, persona_id)
        if not persona:
            raise NotFoundError("Persona", str(persona_id))
        if persona.user_id != user_id:
            raise ForbiddenError("Access denied")
        return persona
