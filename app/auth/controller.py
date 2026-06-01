import hashlib
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from jose import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, _bearer
from app.auth.db_models import User
from app.auth.manager import AuthManager
from app.auth.models import LoginRequest, RefreshRequest, RegisterRequest, TokenResponse, UserResponse, UserUpdate
from app.common.api_response import fail, ok
from app.common.exceptions import AppError
from app.common.redis_client import get_async_redis
from config.settings import get_settings
from database.session import get_db

router = APIRouter()
settings = get_settings()
logger = logging.getLogger(__name__)


@router.post("/register", response_model=None)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    try:
        manager = AuthManager(db)
        tokens = await manager.register(body.email, body.password)
        return ok(tokens.model_dump(), status_code=201)
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.post("/login", response_model=None)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    try:
        manager = AuthManager(db)
        tokens = await manager.login(body.email, body.password)
        return ok(tokens.model_dump())
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.post("/refresh", response_model=None)
async def refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    try:
        manager = AuthManager(db)
        tokens = await manager.refresh(body.refresh_token)
        return ok(tokens.model_dump())
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.post("/logout", response_model=None)
async def logout(
    body: RefreshRequest,
    credentials=Depends(_bearer),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = AuthManager(db)
        await manager.logout(body.refresh_token, current_user.id)

        # Blacklist the access token
        access_token = credentials.credentials
        try:
            payload = jwt.decode(access_token, settings.JWT_SECRET, algorithms=["HS256"])
            exp = payload.get("exp", 0)
            ttl = max(0, int(exp - datetime.now(timezone.utc).timestamp()))
            if ttl > 0:
                token_hash = hashlib.sha256(access_token.encode()).hexdigest()
                redis = get_async_redis()
                await redis.setex(f"auth:blacklist:{token_hash}", ttl, "1")
        except Exception:
            pass  # best-effort blacklist

        return ok(message="Logged out")
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.get("/me", response_model=None)
async def me(current_user: User = Depends(get_current_user)) -> JSONResponse:
    return ok(UserResponse.model_validate(current_user).model_dump(mode="json"))


@router.patch("/me", response_model=None)
async def update_me(
    body: UserUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        if body.timezone is not None:
            current_user.timezone = body.timezone
        if body.vinu_agent_name is not None:
            current_user.vinu_agent_name = body.vinu_agent_name
        await db.commit()
        await db.refresh(current_user)
        return ok(UserResponse.model_validate(current_user).model_dump(mode="json"))
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.get("/me/stats", response_model=None)
async def get_my_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    from sqlalchemy import func, select
    from app.agents.db_models import Agent
    from app.chat.db_models import Conversation, Message
    from app.connectors.db_models import ConnectorInstance
    from app.knowledge_bases.db_models import KnowledgeBase
    from app.website_collections.db_models import WebsiteCollection
    from app.workspaces.db_models import Workspace

    uid = current_user.id
    ws_count    = await db.scalar(select(func.count()).select_from(Workspace).where(Workspace.user_id == uid, Workspace.deleted_at.is_(None)))
    agent_count = await db.scalar(select(func.count(Agent.id)).join(Workspace, Agent.workspace_id == Workspace.id).where(Workspace.user_id == uid, Workspace.deleted_at.is_(None)))
    conv_count  = await db.scalar(select(func.count()).select_from(Conversation).where(Conversation.user_id == uid))
    msg_count   = await db.scalar(select(func.count()).select_from(Message).join(Conversation, Message.conversation_id == Conversation.id).where(Conversation.user_id == uid))
    total_cost  = await db.scalar(select(func.sum(Message.total_cost_usd)).join(Conversation, Message.conversation_id == Conversation.id).where(Conversation.user_id == uid))
    kb_count    = await db.scalar(select(func.count()).select_from(KnowledgeBase).where(KnowledgeBase.user_id == uid))
    wc_count    = await db.scalar(select(func.count()).select_from(WebsiteCollection).where(WebsiteCollection.user_id == uid))
    conn_count  = await db.scalar(select(func.count()).select_from(ConnectorInstance).where(ConnectorInstance.user_id == uid))

    return ok({
        "workspaces": ws_count or 0,
        "agents": agent_count or 0,
        "conversations": conv_count or 0,
        "messages": msg_count or 0,
        "total_cost_usd": round(total_cost or 0.0, 4),
        "knowledge_bases": kb_count or 0,
        "website_collections": wc_count or 0,
        "active_connectors": conn_count or 0,
    })


@router.get("/me/recent-conversations", response_model=None)
async def get_recent_conversations(
    limit: int = Query(10, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    from sqlalchemy import select
    from app.chat.db_models import Conversation

    rows = list(await db.scalars(
        select(Conversation)
        .where(Conversation.user_id == current_user.id)
        .order_by(Conversation.created_at.desc())
        .limit(limit)
    ))
    return ok([{
        "id": str(c.id), "title": c.title,
        "workspace_id": str(c.workspace_id), "created_at": c.created_at.isoformat(),
    } for c in rows])
