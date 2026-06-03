import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.manager import AdminManager
from app.admin.models import AdminUserUpdateRequest
from app.auth.db_models import RoleEnum, User
from app.auth.dependencies import get_current_user_with_roles
from app.common.api_response import fail, ok
from app.common.exceptions import AppError
from database.session import get_db

router = APIRouter()
logger = logging.getLogger(__name__)
_require_admin = get_current_user_with_roles([RoleEnum.admin])


def _manager(db: AsyncSession) -> AdminManager:
    return AdminManager(db)


@router.get("/stats", response_model=None)
async def get_stats(
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        return ok(await _manager(db).get_stats())
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.patch("/users/{user_id}", response_model=None)
async def update_user(
    user_id: UUID,
    body: AdminUserUpdateRequest,
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        return ok(await _manager(db).update_user(user_id, body))
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


async def _list_table(table: str, cursor: str | None, limit: int, db: AsyncSession) -> JSONResponse:
    return ok(await _manager(db).list_table(table, cursor, limit))


async def _list_junction(table: str, limit: int, db: AsyncSession) -> JSONResponse:
    return ok(await _manager(db).list_junction(table, limit))


@router.get("/users", response_model=None)
async def list_all_users(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _list_table("users", cursor, limit, db)


@router.get("/workspaces", response_model=None)
async def list_all_workspaces(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _list_table("workspaces", cursor, limit, db)


@router.get("/conversations", response_model=None)
async def list_all_conversations(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _list_table("conversations", cursor, limit, db)


@router.get("/agents", response_model=None)
async def list_all_agents(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _list_table("agents", cursor, limit, db)


@router.get("/personas", response_model=None)
async def list_all_personas(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _list_table("personas", cursor, limit, db)


@router.get("/messages", response_model=None)
async def list_all_messages(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _list_table("messages", cursor, limit, db)


@router.get("/conversation-summaries", response_model=None)
async def list_all_conversation_summaries(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _list_table("conversation-summaries", cursor, limit, db)


@router.get("/hitl-requests", response_model=None)
async def list_all_hitl_requests(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _list_table("hitl-requests", cursor, limit, db)


@router.get("/message-artifacts", response_model=None)
async def list_all_message_artifacts(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _list_table("message-artifacts", cursor, limit, db)


@router.get("/knowledge-bases", response_model=None)
async def list_all_knowledge_bases(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _list_table("knowledge-bases", cursor, limit, db)


@router.get("/kb-documents", response_model=None)
async def list_all_kb_documents(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _list_table("kb-documents", cursor, limit, db)


@router.get("/website-collections", response_model=None)
async def list_all_website_collections(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _list_table("website-collections", cursor, limit, db)


@router.get("/website-urls", response_model=None)
async def list_all_website_urls(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _list_table("website-urls", cursor, limit, db)


@router.get("/connector-definitions", response_model=None)
async def list_all_connector_definitions(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _list_table("connector-definitions", cursor, limit, db)


@router.get("/connector-instances", response_model=None)
async def list_all_connector_instances(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _list_table("connector-instances", cursor, limit, db)


@router.get("/api-keys", response_model=None)
async def list_all_api_keys(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _list_table("api-keys", cursor, limit, db)


@router.get("/long-term-memory", response_model=None)
async def list_all_long_term_memory(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _list_table("long-term-memory", cursor, limit, db)


@router.get("/refresh-tokens", response_model=None)
async def list_all_refresh_tokens(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _list_table("refresh-tokens", cursor, limit, db)


@router.get("/agent-kbs", response_model=None)
async def list_agent_kbs(
    limit: int = Query(500, ge=1, le=1000),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _list_junction("agent-kbs", limit, db)


@router.get("/agent-personas", response_model=None)
async def list_agent_personas(
    limit: int = Query(500, ge=1, le=1000),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _list_junction("agent-personas", limit, db)


@router.get("/agent-website-collections", response_model=None)
async def list_agent_website_collections(
    limit: int = Query(500, ge=1, le=1000),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _list_junction("agent-website-collections", limit, db)
