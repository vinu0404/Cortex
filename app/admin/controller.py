import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.db_models import RoleEnum, User
from app.auth.dependencies import get_current_user_with_roles
from app.chat.db_models import Conversation, Message
from app.common.api_response import fail, ok
from app.common.exceptions import AppError
from app.common.pagination import build_cursor_page, decode_cursor
from app.workspaces.db_models import Workspace
from database.session import get_db

router = APIRouter()
logger = logging.getLogger(__name__)
_require_admin = get_current_user_with_roles([RoleEnum.admin])


@router.get("/users", response_model=None)
async def list_all_users(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        cursor_created_at, cursor_id = decode_cursor(cursor) if cursor else (None, None)
        query = (
            select(User)
            .order_by(User.created_at.desc(), User.id.desc())
            .limit(limit + 1)
        )
        if cursor_created_at and cursor_id:
            query = query.where(
                (User.created_at < cursor_created_at)
                | ((User.created_at == cursor_created_at) & (User.id < cursor_id))
            )
        users = list(await db.scalars(query))
        page = build_cursor_page(users, limit)
        return ok({
            "items": [_user_dict(u) for u in page.items],
            "next_cursor": page.next_cursor,
            "has_next": page.has_next,
        })
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.get("/workspaces", response_model=None)
async def list_all_workspaces(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        cursor_created_at, cursor_id = decode_cursor(cursor) if cursor else (None, None)
        query = (
            select(Workspace)
            .where(Workspace.deleted_at.is_(None))
            .order_by(Workspace.created_at.desc(), Workspace.id.desc())
            .limit(limit + 1)
        )
        if cursor_created_at and cursor_id:
            query = query.where(
                (Workspace.created_at < cursor_created_at)
                | ((Workspace.created_at == cursor_created_at) & (Workspace.id < cursor_id))
            )
        workspaces = list(await db.scalars(query))
        page = build_cursor_page(workspaces, limit)
        return ok({
            "items": [{"id": str(w.id), "user_id": str(w.user_id), "name": w.name, "created_at": w.created_at.isoformat()} for w in page.items],
            "next_cursor": page.next_cursor,
            "has_next": page.has_next,
        })
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.get("/conversations", response_model=None)
async def list_all_conversations(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        cursor_created_at, cursor_id = decode_cursor(cursor) if cursor else (None, None)
        query = (
            select(Conversation)
            .order_by(Conversation.created_at.desc(), Conversation.id.desc())
            .limit(limit + 1)
        )
        if cursor_created_at and cursor_id:
            query = query.where(
                (Conversation.created_at < cursor_created_at)
                | ((Conversation.created_at == cursor_created_at) & (Conversation.id < cursor_id))
            )
        convs = list(await db.scalars(query))
        page = build_cursor_page(convs, limit)
        return ok({
            "items": [{"id": str(c.id), "user_id": str(c.user_id), "workspace_id": str(c.workspace_id), "title": c.title, "created_at": c.created_at.isoformat()} for c in page.items],
            "next_cursor": page.next_cursor,
            "has_next": page.has_next,
        })
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.get("/stats", response_model=None)
async def get_stats(
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        user_count = await db.scalar(select(func.count()).select_from(User))
        ws_count = await db.scalar(select(func.count()).select_from(Workspace).where(Workspace.deleted_at.is_(None)))
        conv_count = await db.scalar(select(func.count()).select_from(Conversation))
        msg_count = await db.scalar(select(func.count()).select_from(Message))
        return ok({
            "total_users": user_count or 0,
            "total_workspaces": ws_count or 0,
            "total_conversations": conv_count or 0,
            "total_messages": msg_count or 0,
        })
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


def _user_dict(u: User) -> dict:
    return {
        "id": str(u.id),
        "email": u.email,
        "role": u.role.value,
        "is_active": u.is_active,
        "created_at": u.created_at.isoformat(),
    }
