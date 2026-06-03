import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import ForbiddenError, NotFoundError
from app.personas.db_models import Persona, PersonaModelService

logger = logging.getLogger(__name__)


class PersonaManager:
    def __init__(self, db: AsyncSession):
        self._persona_model_service = PersonaModelService(db)

    async def list_personas(self, user_id: UUID) -> list[Persona]:
        return await self._persona_model_service.list_personas(user_id)

    async def create_persona(self, user_id: UUID, name: str, description: str | None, system_prompt: str) -> Persona:
        return await self._persona_model_service.create_persona(
            user_id=user_id, name=name, description=description, system_prompt=system_prompt
        )

    async def update_persona(self, persona_id: UUID, user_id: UUID, **kwargs) -> Persona:
        persona = await self._get_owned(persona_id, user_id)
        return await self._persona_model_service.update_persona_fields(persona, **kwargs)

    async def delete_persona(self, persona_id: UUID, user_id: UUID) -> None:
        persona = await self._get_owned(persona_id, user_id)
        await self._persona_model_service.delete_persona(persona)

    async def _get_owned(self, persona_id: UUID, user_id: UUID) -> Persona:
        persona = await self._persona_model_service.get_persona(persona_id)
        if not persona:
            raise NotFoundError("Persona", str(persona_id))
        if persona.user_id != user_id:
            raise ForbiddenError("Access denied")
        return persona
