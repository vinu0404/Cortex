import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.models import AdminUserUpdateRequest
from app.agents.db_models import Agent
from app.api_keys.db_models import UserApiKey
from app.auth.db_models import RefreshToken, RoleEnum, User
from app.auth.dependencies import get_current_user_with_roles
from app.chat.db_models import (
    Conversation,
    ConversationSummary,
    HitlRequest,
    Message,
    MessageArtifact,
    UserLongTermMemory,
)
from app.common.api_response import fail, ok
from app.common.exceptions import AppError
from app.common.pagination import build_cursor_page, decode_cursor
from app.connectors.db_models import ConnectorDefinition, ConnectorInstance
from app.knowledge_bases.db_models import AgentKnowledgeBase, KbDocument, KnowledgeBase
from app.personas.db_models import AgentPersona, Persona
from app.website_collections.db_models import AgentWebsiteCollection, WebsiteCollection, WebsiteUrl
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
    c_at, c_id = decode_cursor(cursor) if cursor else (None, None)
    items = list(await db.scalars(_cursor_query(User, c_at, c_id, limit)))
    return _page(items, limit, _user_dict)


@router.get("/workspaces", response_model=None)
async def list_all_workspaces(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    c_at, c_id = decode_cursor(cursor) if cursor else (None, None)
    q = _cursor_query(Workspace, c_at, c_id, limit).where(Workspace.deleted_at.is_(None))
    items = list(await db.scalars(q))
    return _page(items, limit, lambda w: {
        "id": str(w.id), "user_id": str(w.user_id), "name": w.name,
        "created_at": w.created_at.isoformat(),
    })


@router.get("/conversations", response_model=None)
async def list_all_conversations(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    c_at, c_id = decode_cursor(cursor) if cursor else (None, None)
    items = list(await db.scalars(_cursor_query(Conversation, c_at, c_id, limit)))
    return _page(items, limit, lambda c: {
        "id": str(c.id), "user_id": str(c.user_id), "workspace_id": str(c.workspace_id),
        "title": c.title, "created_at": c.created_at.isoformat(),
    })


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


@router.patch("/users/{user_id}", response_model=None)
async def update_user(
    user_id: UUID,
    body: AdminUserUpdateRequest,
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        user = await db.get(User, user_id)
        if not user:
            return fail("NOT_FOUND", "User not found", 404)
        if body.is_active is not None:
            user.is_active = body.is_active
        if body.role is not None:
            user.role = body.role
        await db.commit()
        return ok(_user_dict(user))
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


# ---- Cursor-paginated table endpoints ----

def _cursor_query(model, cursor_created_at, cursor_id, limit):
    q = select(model).order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)
    if cursor_created_at and cursor_id:
        q = q.where(
            (model.created_at < cursor_created_at)
            | ((model.created_at == cursor_created_at) & (model.id < cursor_id))
        )
    return q


def _page(items, limit, row_fn):
    page = build_cursor_page(items, limit)
    return ok({"items": [row_fn(i) for i in page.items], "next_cursor": page.next_cursor, "has_next": page.has_next})


@router.get("/agents", response_model=None)
async def list_all_agents(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    c_at, c_id = decode_cursor(cursor) if cursor else (None, None)
    items = list(await db.scalars(_cursor_query(Agent, c_at, c_id, limit)))
    return _page(items, limit, lambda a: {
        "id": str(a.id), "workspace_id": str(a.workspace_id), "user_id": str(a.user_id),
        "name": a.name, "agent_type": a.agent_type.value, "model_id": a.model_id,
        "deleted_at": a.deleted_at.isoformat() if a.deleted_at else None,
        "created_at": a.created_at.isoformat(),
    })


@router.get("/personas", response_model=None)
async def list_all_personas(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    c_at, c_id = decode_cursor(cursor) if cursor else (None, None)
    items = list(await db.scalars(_cursor_query(Persona, c_at, c_id, limit)))
    return _page(items, limit, lambda p: {
        "id": str(p.id), "user_id": str(p.user_id), "name": p.name, "created_at": p.created_at.isoformat(),
    })


@router.get("/messages", response_model=None)
async def list_all_messages(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    c_at, c_id = decode_cursor(cursor) if cursor else (None, None)
    items = list(await db.scalars(_cursor_query(Message, c_at, c_id, limit)))
    return _page(items, limit, lambda m: {
        "id": str(m.id), "conversation_id": str(m.conversation_id), "role": m.role.value,
        "content": m.content[:100], "total_cost_usd": m.total_cost_usd,
        "latency_ms": m.latency_ms, "created_at": m.created_at.isoformat(),
    })


@router.get("/conversation-summaries", response_model=None)
async def list_all_conversation_summaries(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    c_at, c_id = decode_cursor(cursor) if cursor else (None, None)
    items = list(await db.scalars(_cursor_query(ConversationSummary, c_at, c_id, limit)))
    return _page(items, limit, lambda s: {
        "id": str(s.id), "conversation_id": str(s.conversation_id),
        "message_range_start": s.message_range_start, "message_range_end": s.message_range_end,
        "created_at": s.created_at.isoformat(),
    })


@router.get("/hitl-requests", response_model=None)
async def list_all_hitl_requests(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    c_at, c_id = decode_cursor(cursor) if cursor else (None, None)
    items = list(await db.scalars(_cursor_query(HitlRequest, c_at, c_id, limit)))
    return _page(items, limit, lambda h: {
        "id": str(h.id), "conversation_id": str(h.conversation_id), "agent_id": h.agent_id,
        "tool_names": h.tool_names, "status": h.status.value,
        "expires_at": h.expires_at.isoformat(), "created_at": h.created_at.isoformat(),
    })


@router.get("/message-artifacts", response_model=None)
async def list_all_message_artifacts(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    c_at, c_id = decode_cursor(cursor) if cursor else (None, None)
    items = list(await db.scalars(_cursor_query(MessageArtifact, c_at, c_id, limit)))
    return _page(items, limit, lambda a: {
        "id": str(a.id), "message_id": str(a.message_id), "conversation_id": str(a.conversation_id),
        "user_id": str(a.user_id), "type": a.type, "title": a.title,
        "filename": a.filename, "created_at": a.created_at.isoformat(),
    })


@router.get("/knowledge-bases", response_model=None)
async def list_all_knowledge_bases(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    c_at, c_id = decode_cursor(cursor) if cursor else (None, None)
    items = list(await db.scalars(_cursor_query(KnowledgeBase, c_at, c_id, limit)))
    return _page(items, limit, lambda k: {
        "id": str(k.id), "user_id": str(k.user_id), "name": k.name,
        "document_count": k.document_count, "created_at": k.created_at.isoformat(),
    })


@router.get("/kb-documents", response_model=None)
async def list_all_kb_documents(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    c_at, c_id = decode_cursor(cursor) if cursor else (None, None)
    items = list(await db.scalars(_cursor_query(KbDocument, c_at, c_id, limit)))
    return _page(items, limit, lambda d: {
        "id": str(d.id), "kb_id": str(d.kb_id), "user_id": str(d.user_id),
        "filename": d.filename, "processing_status": d.processing_status.value,
        "chunk_count": d.chunk_count, "created_at": d.created_at.isoformat(),
    })


@router.get("/website-collections", response_model=None)
async def list_all_website_collections(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    c_at, c_id = decode_cursor(cursor) if cursor else (None, None)
    items = list(await db.scalars(_cursor_query(WebsiteCollection, c_at, c_id, limit)))
    return _page(items, limit, lambda w: {
        "id": str(w.id), "user_id": str(w.user_id), "name": w.name,
        "url_count": w.url_count, "created_at": w.created_at.isoformat(),
    })


@router.get("/website-urls", response_model=None)
async def list_all_website_urls(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    c_at, c_id = decode_cursor(cursor) if cursor else (None, None)
    items = list(await db.scalars(_cursor_query(WebsiteUrl, c_at, c_id, limit)))
    return _page(items, limit, lambda u: {
        "id": str(u.id), "collection_id": str(u.collection_id), "user_id": str(u.user_id),
        "url": u.url, "crawl_status": u.crawl_status.value,
        "page_count": u.page_count, "chunk_count": u.chunk_count, "created_at": u.created_at.isoformat(),
    })


@router.get("/connector-definitions", response_model=None)
async def list_all_connector_definitions(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    c_at, c_id = decode_cursor(cursor) if cursor else (None, None)
    items = list(await db.scalars(_cursor_query(ConnectorDefinition, c_at, c_id, limit)))
    return _page(items, limit, lambda d: {
        "id": str(d.id), "slug": d.slug, "display_name": d.display_name,
        "auth_type": d.auth_type.value, "is_active": d.is_active, "created_at": d.created_at.isoformat(),
    })


@router.get("/connector-instances", response_model=None)
async def list_all_connector_instances(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    c_at, c_id = decode_cursor(cursor) if cursor else (None, None)
    items = list(await db.scalars(_cursor_query(ConnectorInstance, c_at, c_id, limit)))
    return _page(items, limit, lambda i: {
        "id": str(i.id), "user_id": str(i.user_id), "definition_id": str(i.definition_id),
        "account_label": i.account_label, "status": i.status.value,
        "token_expires_at": i.token_expires_at.isoformat() if i.token_expires_at else None,
        "created_at": i.created_at.isoformat(),
    })


@router.get("/api-keys", response_model=None)
async def list_all_api_keys(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    c_at, c_id = decode_cursor(cursor) if cursor else (None, None)
    items = list(await db.scalars(_cursor_query(UserApiKey, c_at, c_id, limit)))
    return _page(items, limit, lambda k: {
        "id": str(k.id), "user_id": str(k.user_id), "key_name": k.key_name,
        "provider": k.provider, "created_at": k.created_at.isoformat(),
    })


@router.get("/long-term-memory", response_model=None)
async def list_all_long_term_memory(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    c_at, c_id = decode_cursor(cursor) if cursor else (None, None)
    items = list(await db.scalars(_cursor_query(UserLongTermMemory, c_at, c_id, limit)))
    return _page(items, limit, lambda m: {
        "id": str(m.id), "user_id": str(m.user_id),
        "critical_facts": str(m.critical_facts)[:200], "preferences": str(m.preferences)[:200],
        "updated_at": m.updated_at.isoformat(),
    })


@router.get("/refresh-tokens", response_model=None)
async def list_all_refresh_tokens(
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    c_at, c_id = decode_cursor(cursor) if cursor else (None, None)
    items = list(await db.scalars(_cursor_query(RefreshToken, c_at, c_id, limit)))
    return _page(items, limit, lambda t: {
        "id": str(t.id), "user_id": str(t.user_id),
        "expires_at": t.expires_at.isoformat(),
        "revoked_at": t.revoked_at.isoformat() if t.revoked_at else None,
        "created_at": t.created_at.isoformat(),
    })


# ---- Junction tables (no cursor, simple limit) ----

@router.get("/agent-kbs", response_model=None)
async def list_agent_kbs(
    limit: int = Query(500, ge=1, le=1000),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    rows = list(await db.scalars(select(AgentKnowledgeBase).limit(limit)))
    return ok({"items": [{"agent_id": str(r.agent_id), "kb_id": str(r.kb_id)} for r in rows]})


@router.get("/agent-personas", response_model=None)
async def list_agent_personas(
    limit: int = Query(500, ge=1, le=1000),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    rows = list(await db.scalars(select(AgentPersona).limit(limit)))
    return ok({"items": [{"agent_id": str(r.agent_id), "persona_id": str(r.persona_id)} for r in rows]})


@router.get("/agent-website-collections", response_model=None)
async def list_agent_website_collections(
    limit: int = Query(500, ge=1, le=1000),
    _admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    rows = list(await db.scalars(select(AgentWebsiteCollection).limit(limit)))
    return ok({"items": [{"agent_id": str(r.agent_id), "collection_id": str(r.collection_id)} for r in rows]})


def _user_dict(u: User) -> dict:
    return {
        "id": str(u.id),
        "email": u.email,
        "role": u.role.value,
        "is_active": u.is_active,
        "created_at": u.created_at.isoformat(),
    }
