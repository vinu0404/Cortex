"""Embed endpoints — no JWT auth, embed_token IS the credential."""
import json as _json
import logging
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from app.chat.streaming import chat_stream
from app.common.exceptions import NotFoundError
from app.common.redis_client import get_async_redis
from app.workspaces.manager import WorkspaceManager
from config.settings import get_settings
from database.session import get_custom_db_context_session

router = APIRouter()
logger = logging.getLogger(__name__)
settings = get_settings()

_RATE_LIMIT_WINDOW = 3600  # 1 hour


class EmbedStreamRequest(BaseModel):
    query: str
    conversation_id: UUID | None = None


async def _enforce_budget_limit(
    workspace_id: UUID,
    threshold: float | int | None,
    spend: float | int,
    message: str,
) -> JSONResponse | None:
    """Return 429 JSONResponse and auto-disable embed if threshold is exceeded, else None."""
    if not threshold or spend < threshold:
        return None
    try:
        async with get_custom_db_context_session() as db:
            await WorkspaceManager(db).auto_disable_embed(workspace_id)
    except Exception:
        logger.exception("Failed to auto-disable embed for workspace %s", workspace_id)
    return JSONResponse(
        {"status": "error", "code": "BUDGET_EXCEEDED", "message": message},
        status_code=429,
    )


@router.get("/embed/{token}")
async def serve_embed(token: str) -> Response:
    async with get_custom_db_context_session() as db:
        try:
            await WorkspaceManager(db).get_workspace_by_embed_token(token)
        except NotFoundError:
            return JSONResponse({"status": "error", "code": "NOT_FOUND", "message": "Embed not found"}, status_code=404)
    return FileResponse("frontend/embed.html")


@router.post("/embed/{token}/stream")
async def embed_stream(token: str, body: EmbedStreamRequest, request: Request) -> StreamingResponse:
    # Rate limit: 50 req/hour per (token, client_ip)
    client_ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown")
    rl_key = f"embed_rl:{token}:{client_ip}"
    redis = get_async_redis()
    try:
        count = await redis.incr(rl_key)
        if count == 1:
            await redis.expire(rl_key, _RATE_LIMIT_WINDOW)
        if count > settings.EMBED_RATE_LIMIT_PER_HOUR:
            return JSONResponse(
                {"status": "error", "code": "RATE_LIMITED", "message": "Too many requests. Please try again later."},
                status_code=429,
            )
    except Exception:
        logger.warning("Redis rate-limit check failed for embed token %s — allowing request", token)

    # Validate embed token, load workspace + owner user
    try:
        async with get_custom_db_context_session() as db:
            ws, owner = await WorkspaceManager(db).get_workspace_by_embed_token(token)
            workspace_id = ws.id
            user_id = owner.id
            hitl_auto_approve = ws.embed_hitl_auto_approve
            budget_usd = ws.embed_budget_usd
            budget_tokens = ws.embed_budget_tokens
            spend_usd = ws.embed_spend_usd
            spend_tokens = ws.embed_spend_tokens
    except NotFoundError:
        return JSONResponse({"status": "error", "code": "NOT_FOUND", "message": "Embed not found"}, status_code=404)
    except Exception:
        logger.exception("Failed to load embed workspace for token %s", token)
        return JSONResponse({"status": "error", "code": "INTERNAL_ERROR", "message": "Internal error"}, status_code=500)

    # Check embed-specific budget limits before starting stream
    if resp := await _enforce_budget_limit(workspace_id, budget_usd, spend_usd, "Embed cost budget exceeded."):
        return resp
    if resp := await _enforce_budget_limit(workspace_id, budget_tokens, spend_tokens, "Embed token limit reached."):
        return resp

    # Create or reuse conversation
    conversation_id = body.conversation_id
    if not conversation_id:
        async with get_custom_db_context_session() as db:
            from app.chat.manager import ChatManager
            conv = await ChatManager(db).create_conversation(workspace_id, user_id)
            conversation_id = conv.id
    else:
        # Verify conversation belongs to this workspace
        async with get_custom_db_context_session() as db:
            from app.chat.db_models import Conversation
            from sqlalchemy import select
            conv = await db.scalar(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.workspace_id == workspace_id,
                )
            )
            if not conv:
                # Conversation mismatch — create fresh one
                from app.chat.manager import ChatManager
                new_conv = await ChatManager(db).create_conversation(workspace_id, user_id)
                conversation_id = new_conv.id

    async def _spend_tracking_stream():
        done_message_id = None
        async for chunk in chat_stream(
            request=request,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            query=body.query,
            user_id=user_id,
            persona_id=None,
            is_embed=True,
            embed_hitl_auto_approve=hitl_auto_approve,
        ):
            if not done_message_id and 'event: done' in chunk:
                for line in chunk.splitlines():
                    if line.startswith('data: '):
                        try:
                            done_message_id = _json.loads(line[6:]).get('message_id')
                        except Exception:
                            logger.debug("Failed to parse done event chunk", exc_info=True)
            yield chunk

        # After stream: update workspace embed spend from saved message
        if done_message_id:
            try:
                async with get_custom_db_context_session() as db:
                    from app.chat.db_models import Message
                    msg = await db.get(Message, UUID(done_message_id))
                    if msg:
                        cost = msg.total_cost_usd or 0.0
                        tokens = (msg.token_details or {}).get("total_tokens", 0)
                        if cost > 0 or tokens > 0:
                            await WorkspaceManager(db).increment_embed_spend(workspace_id, cost, tokens)
            except Exception:
                logger.exception("Failed to update embed spend for workspace %s", workspace_id)

    return StreamingResponse(
        _spend_tracking_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )
