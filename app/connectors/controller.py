import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.db_models import User
from app.common.api_response import fail, ok
from app.common.exceptions import AppError
from app.connectors.manager import ConnectorManager
from app.connectors.models import ConnectorDefinitionResponse, CredentialsConnectRequest
from database.session import get_db

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/definitions", response_model=None)
async def list_definitions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = ConnectorManager(db)
        definitions = await manager.list_definitions()
        return ok([ConnectorDefinitionResponse.model_validate(d).model_dump(mode="json") for d in definitions])
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.get("/instances", response_model=None)
async def list_instances(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = ConnectorManager(db)
        instances = await manager.list_user_instances(current_user.id)
        result = []
        for inst in instances:
            result.append({
                "id": str(inst.id),
                "user_id": str(inst.user_id),
                "definition_id": str(inst.definition_id),
                "account_label": inst.account_label,
                "status": inst.status.value,
                "slug": inst.definition.slug,
                "display_name": inst.definition.display_name,
                "created_at": inst.created_at.isoformat(),
            })
        return ok(result)
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.post("/{slug}/connect", response_model=None)
async def connect_credentials(
    slug: str,
    body: CredentialsConnectRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        mgr = ConnectorManager(db)
        instance = await mgr.connect_credentials(
            user_id=current_user.id,
            slug=slug,
            connection_string=body.connection_string,
            db_type=body.db_type,
            label=body.label,
        )
        await db.commit()
        return ok({"id": str(instance.id), "slug": slug, "account_label": instance.account_label}, status_code=201)
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.get("/{slug}/auth-url", response_model=None)
async def get_auth_url(
    slug: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = ConnectorManager(db)
        auth_url, state = await manager.get_auth_url(slug, current_user.id)
        return ok({"auth_url": auth_url, "state": state})
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.get("/callback", response_model=None, include_in_schema=False)
async def oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = ConnectorManager(db)
        instance = await manager.handle_callback(code, state)
        return ok({"connected": True, "instance_id": str(instance.id)})
    except AppError as e:
        return fail(e.code, e.message, e.status_code)
    except Exception as e:
        logger.error("OAuth callback error: %s", e)
        return fail("OAUTH_ERROR", "OAuth callback failed", 500)


@router.delete("/instances/{instance_id}", response_model=None)
async def delete_instance(
    instance_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = ConnectorManager(db)
        await manager.delete_instance(instance_id, current_user.id)
        return ok(message="Connector disconnected")
    except AppError as e:
        return fail(e.code, e.message, e.status_code)
