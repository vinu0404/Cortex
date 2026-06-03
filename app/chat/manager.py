import logging
import asyncio
import base64
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.models import ArtifactSaveRequest, ArtifactSaveResponse
from app.chat.db_models import (
    ChatModelService,
    Conversation,
    HitlRequest,
    HitlStatusEnum,
    Message,
    MessageArtifact,
    MessageRoleEnum,
)
from app.common.redis_client import get_async_redis
from app.common.retry import async_redis_call
from config.settings import get_settings
from document_pipeline.storage import build_artifact_storage_key, generate_presigned_url, upload_bytes

settings = get_settings()
logger = logging.getLogger(__name__)


class ChatManager:
    def __init__(self, db: AsyncSession):
        self._db = db
        self._chat_model_service = ChatModelService(db)

    async def create_conversation(self, workspace_id: UUID, user_id: UUID) -> Conversation:
        return await self._chat_model_service.create_conversation(workspace_id, user_id)

    async def list_conversations(
        self,
        workspace_id: UUID,
        user_id: UUID,
        limit: int,
        cursor_created_at: datetime | None,
        cursor_id: UUID | None,
    ) -> list[Conversation]:
        return await self._chat_model_service.list_conversations(
            workspace_id, user_id, limit, cursor_created_at, cursor_id
        )

    async def get_conversation(self, conversation_id: UUID, user_id: UUID) -> Conversation:
        return await self._chat_model_service.get_user_conversation(conversation_id, user_id)

    async def delete_conversation(self, conversation_id: UUID, user_id: UUID) -> None:
        await self._chat_model_service.delete_conversation(conversation_id, user_id)
        await self._chat_model_service.save_changes()

    async def get_messages(
        self,
        conversation_id: UUID,
        user_id: UUID,
        cursor_created_at: datetime | None = None,
        cursor_id: UUID | None = None,
        limit: int = 30,
    ) -> tuple[list[Message], bool]:
        await self.get_conversation(conversation_id, user_id)
        return await self._chat_model_service.get_messages(
            conversation_id, cursor_created_at, cursor_id, limit
        )

    async def add_message(
        self,
        conversation_id: UUID,
        role: MessageRoleEnum,
        content: str,
        token_details: dict | None = None,
        total_cost_usd: float | None = None,
        latency_ms: int | None = None,
        langfuse_trace_id: str | None = None,
        sources: list[dict] | None = None,
    ) -> Message:
        return await self._chat_model_service.add_message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            token_details=token_details,
            total_cost_usd=total_cost_usd,
            latency_ms=latency_ms,
            langfuse_trace_id=langfuse_trace_id,
            sources=sources,
        )

    async def load_memory_context(self, conversation_id: UUID) -> tuple[list[str], list[dict]]:
        return await self._chat_model_service.load_memory_context(
            conversation_id, settings.SHORT_TERM_MEMORY_WINDOW
        )

    async def save_summary(self, conversation_id: UUID, summary: str) -> None:
        await self._chat_model_service.save_summary(
            conversation_id,
            summary,
            settings.SHORT_TERM_COMPRESS_FIRST_N,
        )

    async def get_long_term_memory(self, user_id: UUID):
        return await self._chat_model_service.get_long_term_memory(user_id)

    async def upsert_long_term_memory(
        self, user_id: UUID, critical_facts: dict, preferences: dict
    ) -> None:
        await self._chat_model_service.upsert_long_term_memory(user_id, critical_facts, preferences)

    async def create_hitl_request(
        self,
        conversation_id: UUID,
        agent_id: str,
        tool_names: list[str],
        expires_at: datetime,
    ) -> HitlRequest:
        return await self._chat_model_service.create_hitl_request(
            conversation_id,
            agent_id,
            tool_names,
            expires_at,
        )

    async def resolve_hitl_request(
        self,
        request_id: UUID,
        user_id: UUID,
        approved: bool,
        instructions: str | None,
    ) -> HitlRequest:
        return await self._chat_model_service.resolve_hitl_request(
            request_id, user_id, approved, instructions
        )

    async def update_conversation_title(self, conversation_id: UUID, title: str) -> None:
        await self._chat_model_service.update_conversation_title(conversation_id, title)

    async def save_artifact(
        self,
        message_id: UUID,
        conversation_id: UUID,
        user_id: UUID,
        type: str,
        title: str,
        filename: str,
        storage_key: str,
        content: str | None = None,
    ) -> MessageArtifact:
        return await self._chat_model_service.save_artifact(
            message_id=message_id,
            conversation_id=conversation_id,
            user_id=user_id,
            artifact_type=type,
            title=title,
            filename=filename,
            storage_key=storage_key,
            content=content,
        )

    async def save_user_artifact(self, body: ArtifactSaveRequest, user_id: UUID) -> tuple[ArtifactSaveResponse, bool]:
        await self.get_conversation(body.conversation_id, user_id)
        existing = await self.get_artifact_by_message_and_type(body.message_id, body.type)
        if existing:
            url = await _run_blocking(generate_presigned_url, existing.storage_key)
            return ArtifactSaveResponse(id=existing.id, url=url), False

        raw_bytes, content_type = _artifact_bytes(body)
        storage_key = build_artifact_storage_key(str(body.conversation_id), body.filename)
        await _run_blocking(upload_bytes, raw_bytes, storage_key, content_type)
        url = await _run_blocking(generate_presigned_url, storage_key)
        artifact = await self.save_artifact(
            message_id=body.message_id,
            conversation_id=body.conversation_id,
            user_id=user_id,
            type=body.type,
            title=body.title,
            filename=body.filename,
            storage_key=storage_key,
        )
        await self._chat_model_service.save_changes()
        return ArtifactSaveResponse(id=artifact.id, url=url), True

    async def respond_to_hitl(
        self,
        request_id: UUID,
        user_id: UUID,
        approved: bool,
        instructions: str | None,
    ) -> dict:
        await self.resolve_hitl_request(request_id, user_id, approved, instructions)
        await self._chat_model_service.save_changes()
        redis = get_async_redis()
        payload = json.dumps({
            "approved": approved,
            "instructions": instructions,
            "request_id": str(request_id),
        })
        await async_redis_call(redis, "publish", f"hitl:{request_id}", payload)
        return {"approved": approved}

    async def get_or_create_embed_conversation(
        self,
        conversation_id: UUID | None,
        workspace_id: UUID,
        user_id: UUID,
    ) -> UUID:
        return await self._chat_model_service.get_or_create_embed_conversation(
            conversation_id, workspace_id, user_id
        )

    async def get_message_usage(self, message_id: UUID) -> tuple[float, int]:
        return await self._chat_model_service.get_message_usage(message_id)

    async def get_artifact_by_message_and_type(self, message_id: UUID, type: str) -> MessageArtifact | None:
        return await self._chat_model_service.get_artifact_by_message_and_type(message_id, type)

    async def get_artifacts_for_messages(self, message_ids: list[UUID]) -> dict[UUID, list[MessageArtifact]]:
        return await self._chat_model_service.get_artifacts_for_messages(message_ids)


async def _run_blocking(fn, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn, *args)


def _artifact_bytes(body: ArtifactSaveRequest) -> tuple[bytes, str]:
    if body.type == "pdf":
        return base64.b64decode(body.content), "application/pdf"
    return body.content.encode("utf-8"), "text/csv"
