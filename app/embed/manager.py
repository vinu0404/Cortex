import json as _json
import logging
from uuid import UUID

from fastapi import Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from app.chat.streaming import chat_stream
from app.common.api_response import fail
from app.common.exceptions import NotFoundError
from app.common.redis_client import get_async_redis
from app.common.retry import async_redis_call
from app.embed.db_models import EmbedModelService
from config.settings import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
_RATE_LIMIT_WINDOW = 3600


class EmbedManager:
    def __init__(self):
        self._embed_model_service = EmbedModelService()

    async def serve_embed(self, token: str) -> Response:
        try:
            await self._embed_model_service.get_workspace_and_owner(token)
        except NotFoundError:
            return fail("NOT_FOUND", "Embed not found", 404)
        return FileResponse("frontend/embed.html")

    async def stream_embed(self, token: str, body, request: Request):
        rate_limit = await self._check_rate_limit(token, request)
        if rate_limit is not None:
            return rate_limit

        try:
            ws, owner = await self._embed_model_service.get_workspace_and_owner(token)
        except NotFoundError:
            return fail("NOT_FOUND", "Embed not found", 404)
        except Exception:
            logger.exception("Failed to load embed workspace for token %s", token)
            return fail("INTERNAL_ERROR", "Internal error", 500)

        if resp := await self._enforce_budget_limit(ws.id, ws.embed_budget_usd, ws.embed_spend_usd, "Embed cost budget exceeded."):
            return resp
        if resp := await self._enforce_budget_limit(ws.id, ws.embed_budget_tokens, ws.embed_spend_tokens, "Embed token limit reached."):
            return resp

        conversation_id = await self._embed_model_service.get_or_create_conversation(
            body.conversation_id,
            ws.id,
            owner.id,
        )

        async def _spend_tracking_stream():
            done_message_id = None
            async for chunk in chat_stream(
                request=request,
                workspace_id=ws.id,
                conversation_id=conversation_id,
                query=body.query,
                user_id=owner.id,
                persona_id=None,
                is_embed=True,
                embed_hitl_auto_approve=ws.embed_hitl_auto_approve,
            ):
                if not done_message_id and "event: done" in chunk:
                    for line in chunk.splitlines():
                        if line.startswith("data: "):
                            try:
                                done_message_id = _json.loads(line[6:]).get("message_id")
                            except Exception:
                                logger.debug("Failed to parse done event chunk", exc_info=True)
                yield chunk
            if done_message_id:
                try:
                    await self._embed_model_service.update_spend_from_message(ws.id, UUID(done_message_id))
                except Exception:
                    logger.exception("Failed to update embed spend for workspace %s", ws.id)

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

    async def _check_rate_limit(self, token: str, request: Request):
        client_ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown")
        rl_key = f"embed_rl:{token}:{client_ip}"
        redis = get_async_redis()
        try:
            count = await async_redis_call(redis, "incr", rl_key)
            if count == 1:
                await async_redis_call(redis, "expire", rl_key, _RATE_LIMIT_WINDOW)
            if count > settings.EMBED_RATE_LIMIT_PER_HOUR:
                return fail("RATE_LIMITED", "Too many requests. Please try again later.", 429)
        except Exception:
            logger.warning("Redis rate-limit check failed for embed token %s — allowing request", token)
        return None

    async def _enforce_budget_limit(
        self,
        workspace_id: UUID,
        threshold: float | int | None,
        spend: float | int,
        message: str,
    ):
        if not threshold or spend < threshold:
            return None
        try:
            await self._embed_model_service.auto_disable_embed(workspace_id)
        except Exception:
            logger.exception("Failed to auto-disable embed for workspace %s", workspace_id)
        return fail("BUDGET_EXCEEDED", message, 429)
