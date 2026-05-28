import logging
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.db_models import User
from app.common.api_response import fail, ok
from app.common.exceptions import AppError
from app.personas.manager import PersonaManager
from app.personas.models import PersonaCreate, PersonaResponse, PersonaUpdate
from database.session import get_db

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("", response_model=None)
async def list_personas(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = PersonaManager(db)
        personas = await manager.list_personas(current_user.id)
        return ok([PersonaResponse.model_validate(p).model_dump(mode="json") for p in personas])
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.post("", response_model=None)
async def create_persona(
    body: PersonaCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = PersonaManager(db)
        persona = await manager.create_persona(current_user.id, body.name, body.description, body.system_prompt)
        return ok(PersonaResponse.model_validate(persona).model_dump(mode="json"), status_code=201)
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.put("/{persona_id}", response_model=None)
async def update_persona(
    persona_id: UUID,
    body: PersonaUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = PersonaManager(db)
        persona = await manager.update_persona(persona_id, current_user.id, **body.model_dump(exclude_none=True))
        return ok(PersonaResponse.model_validate(persona).model_dump(mode="json"))
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.delete("/{persona_id}", response_model=None)
async def delete_persona(
    persona_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = PersonaManager(db)
        await manager.delete_persona(persona_id, current_user.id)
        return ok(message="Persona deleted")
    except AppError as e:
        return fail(e.code, e.message, e.status_code)
