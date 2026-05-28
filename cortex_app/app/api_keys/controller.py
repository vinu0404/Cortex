import logging
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_keys.manager import ApiKeyManager, _mask_key
from app.api_keys.models import ApiKeyCreate
from app.auth.dependencies import get_current_user
from app.auth.db_models import User
from app.common.api_response import fail, ok
from app.common.exceptions import AppError
from database.session import get_db

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("", response_model=None)
async def create_api_key(
    body: ApiKeyCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = ApiKeyManager(db)
        key_record = await manager.create_key(current_user.id, body.key_name, body.api_key)
        return ok({
            "id": str(key_record.id),
            "key_name": key_record.key_name,
            "provider": key_record.provider,
            "available_models": key_record.available_models,
            "masked_key": _mask_key(body.api_key),
            "created_at": key_record.created_at.isoformat(),
        }, status_code=201)
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.get("", response_model=None)
async def list_api_keys(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = ApiKeyManager(db)
        keys = await manager.list_keys(current_user.id)
        return ok([{
            "id": str(k.id),
            "key_name": k.key_name,
            "provider": k.provider,
            "available_models": k.available_models,
            "created_at": k.created_at.isoformat(),
        } for k in keys])
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.get("/{key_id}/models", response_model=None)
async def get_models(
    key_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = ApiKeyManager(db)
        models = await manager.get_models(key_id, current_user.id)
        return ok({"models": models})
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.delete("/{key_id}", response_model=None)
async def delete_api_key(
    key_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = ApiKeyManager(db)
        await manager.delete_key(key_id, current_user.id)
        return ok(message="API key deleted")
    except AppError as e:
        return fail(e.code, e.message, e.status_code)
