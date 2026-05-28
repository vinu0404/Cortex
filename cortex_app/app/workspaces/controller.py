import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.db_models import User
from app.common.api_response import fail, ok
from app.common.exceptions import AppError
from app.common.pagination import build_cursor_page, decode_cursor
from app.workspaces.manager import WorkspaceManager
from app.workspaces.models import WorkspaceCreate, WorkspaceResponse, WorkspaceUpdate
from database.session import get_db

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("", response_model=None)
async def list_workspaces(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        cursor_created_at, cursor_id = decode_cursor(cursor) if cursor else (None, None)
        manager = WorkspaceManager(db)
        items = await manager.list_workspaces(current_user.id, limit, cursor_created_at, cursor_id)
        page = build_cursor_page(items, limit)
        return ok({
            "items": [WorkspaceResponse.model_validate(w).model_dump(mode="json") for w in page.items],
            "next_cursor": page.next_cursor,
            "has_next": page.has_next,
        })
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.post("", response_model=None)
async def create_workspace(
    body: WorkspaceCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = WorkspaceManager(db)
        ws = await manager.create_workspace(current_user.id, body.name, body.description)
        return ok(WorkspaceResponse.model_validate(ws).model_dump(mode="json"), status_code=201)
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.get("/{workspace_id}", response_model=None)
async def get_workspace(
    workspace_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = WorkspaceManager(db)
        ws = await manager.get_workspace(workspace_id, current_user.id)
        return ok(WorkspaceResponse.model_validate(ws).model_dump(mode="json"))
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.put("/{workspace_id}", response_model=None)
async def update_workspace(
    workspace_id: UUID,
    body: WorkspaceUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = WorkspaceManager(db)
        ws = await manager.update_workspace(workspace_id, current_user.id, body.name, body.description)
        return ok(WorkspaceResponse.model_validate(ws).model_dump(mode="json"))
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.delete("/{workspace_id}", response_model=None)
async def delete_workspace(
    workspace_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = WorkspaceManager(db)
        await manager.delete_workspace(workspace_id, current_user.id)
        return ok(message="Workspace deleted")
    except AppError as e:
        return fail(e.code, e.message, e.status_code)
