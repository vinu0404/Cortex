import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.db_models import (
    Conversation,
    ConversationSummary,
    HitlRequest,
    HitlStatusEnum,
    Message,
    MessageArtifact,
    MessageRoleEnum,
    UserLongTermMemory,
)
from app.common.exceptions import ForbiddenError, NotFoundError
from config.settings import get_settings
from core.schemas import LongTermMemory

settings = get_settings()
logger = logging.getLogger(__name__)


class ChatManager:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def create_conversation(self, workspace_id: UUID, user_id: UUID) -> Conversation:
        conv = Conversation(workspace_id=workspace_id, user_id=user_id)
        self._db.add(conv)
        await self._db.flush()
        return conv

    async def list_conversations(
        self,
        workspace_id: UUID,
        user_id: UUID,
        limit: int,
        cursor_created_at: datetime | None,
        cursor_id: UUID | None,
    ) -> list[Conversation]:
        query = (
            select(Conversation)
            .where(Conversation.workspace_id == workspace_id)
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.created_at.desc(), Conversation.id.desc())
            .limit(limit + 1)
        )
        if cursor_created_at and cursor_id:
            query = query.where(
                (Conversation.created_at < cursor_created_at)
                | ((Conversation.created_at == cursor_created_at) & (Conversation.id < cursor_id))
            )
        result = await self._db.scalars(query)
        return list(result)

    async def get_conversation(self, conversation_id: UUID, user_id: UUID) -> Conversation:
        conv = await self._db.get(Conversation, conversation_id)
        if not conv:
            raise NotFoundError("Conversation", str(conversation_id))
        if conv.user_id != user_id:
            raise ForbiddenError("Access denied")
        return conv

    async def get_messages(
        self,
        conversation_id: UUID,
        user_id: UUID,
        cursor_created_at: datetime | None = None,
        cursor_id: UUID | None = None,
        limit: int = 30,
    ) -> tuple[list[Message], bool]:
        await self.get_conversation(conversation_id, user_id)
        query = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit + 1)
        )
        if cursor_created_at and cursor_id:
            query = query.where(
                (Message.created_at < cursor_created_at)
                | ((Message.created_at == cursor_created_at) & (Message.id < cursor_id))
            )
        rows = list(await self._db.scalars(query))
        has_more = len(rows) > limit
        return list(reversed(rows[:limit])), has_more

    async def add_message(
        self,
        conversation_id: UUID,
        role: MessageRoleEnum,
        content: str,
        token_details: dict | None = None,
        total_cost_usd: float | None = None,
        latency_ms: int | None = None,
        langfuse_trace_id: str | None = None,
    ) -> Message:
        msg = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            token_details=token_details,
            total_cost_usd=total_cost_usd,
            latency_ms=latency_ms,
            langfuse_trace_id=langfuse_trace_id,
        )
        self._db.add(msg)
        await self._db.flush()
        return msg

    async def load_memory_context(self, conversation_id: UUID) -> tuple[list[str], list[dict]]:
        summaries_result = await self._db.scalars(
            select(ConversationSummary)
            .where(ConversationSummary.conversation_id == conversation_id)
            .order_by(ConversationSummary.created_at.asc())
        )
        summaries = [s.summary for s in summaries_result]

        msgs_result = await self._db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc())
            .limit(settings.SHORT_TERM_MEMORY_WINDOW)
        )
        recent = [{"role": m.role.value, "content": m.content} for m in reversed(list(msgs_result))]
        return summaries, recent

    async def save_summary(
        self, conversation_id: UUID, summary: str, range_start: int, range_end: int
    ) -> None:
        self._db.add(ConversationSummary(
            conversation_id=conversation_id,
            summary=summary,
            message_range_start=range_start,
            message_range_end=range_end,
        ))

    async def get_long_term_memory(self, user_id: UUID) -> LongTermMemory:
        record = await self._db.scalar(
            select(UserLongTermMemory).where(UserLongTermMemory.user_id == user_id)
        )
        if not record:
            return LongTermMemory()
        return LongTermMemory(
            critical_facts=record.critical_facts,
            preferences=record.preferences,
        )

    async def upsert_long_term_memory(
        self, user_id: UUID, critical_facts: dict, preferences: dict
    ) -> None:
        record = await self._db.scalar(
            select(UserLongTermMemory).where(UserLongTermMemory.user_id == user_id)
        )
        if record:
            record.critical_facts = {**record.critical_facts, **critical_facts}
            record.preferences = {**record.preferences, **preferences}
            record.updated_at = datetime.now(timezone.utc)
        else:
            self._db.add(UserLongTermMemory(
                user_id=user_id, critical_facts=critical_facts, preferences=preferences
            ))

    async def create_hitl_request(
        self,
        conversation_id: UUID,
        agent_id: str,
        tool_names: list[str],
        expires_at: datetime,
    ) -> HitlRequest:
        req = HitlRequest(
            conversation_id=conversation_id,
            agent_id=agent_id,
            tool_names=tool_names,
            expires_at=expires_at,
        )
        self._db.add(req)
        await self._db.flush()
        return req

    async def resolve_hitl_request(
        self,
        request_id: UUID,
        user_id: UUID,
        approved: bool,
        instructions: str | None,
    ) -> HitlRequest:
        req = await self._db.get(HitlRequest, request_id)
        if not req:
            raise NotFoundError("HitlRequest", str(request_id))
        conv = await self._db.get(Conversation, req.conversation_id)
        if not conv or conv.user_id != user_id:
            raise ForbiddenError("Access denied")
        req.status = HitlStatusEnum.approved if approved else HitlStatusEnum.denied
        req.user_instructions = instructions
        req.updated_at = datetime.now(timezone.utc)
        await self._db.flush()
        return req

    async def update_conversation_title(self, conversation_id: UUID, title: str) -> None:
        conv = await self._db.get(Conversation, conversation_id)
        if conv:
            conv.title = title
            conv.updated_at = datetime.now(timezone.utc)

    async def save_artifact(
        self,
        message_id: UUID,
        conversation_id: UUID,
        user_id: UUID,
        type: str,
        title: str,
        filename: str,
        storage_key: str,
    ) -> MessageArtifact:
        artifact = MessageArtifact(
            message_id=message_id,
            conversation_id=conversation_id,
            user_id=user_id,
            type=type,
            title=title,
            filename=filename,
            storage_key=storage_key,
        )
        self._db.add(artifact)
        await self._db.flush()
        return artifact

    async def get_artifact_by_message_and_type(self, message_id: UUID, type: str) -> MessageArtifact | None:
        return await self._db.scalar(
            select(MessageArtifact)
            .where(MessageArtifact.message_id == message_id, MessageArtifact.type == type)
        )

    async def get_artifacts_for_messages(self, message_ids: list[UUID]) -> dict[UUID, list[MessageArtifact]]:
        if not message_ids:
            return {}
        rows = list(await self._db.scalars(
            select(MessageArtifact).where(MessageArtifact.message_id.in_(message_ids))
        ))
        result: dict[UUID, list[MessageArtifact]] = {}
        for row in rows:
            result.setdefault(row.message_id, []).append(row)
        return result
