import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, and_, func, select
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, selectinload

from app.common.exceptions import ForbiddenError, NotFoundError
from core.schemas import LongTermMemory

from database.session import Base


class MessageRoleEnum(str, enum.Enum):
    user = "user"
    assistant = "assistant"
    system = "system"


class HitlStatusEnum(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    denied = "denied"
    timed_out = "timed_out"


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_workspace_user_created", "workspace_id", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[MessageRoleEnum] = mapped_column(Enum(MessageRoleEnum, name="messagerolenewnum", create_type=False), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    total_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    langfuse_trace_id: Mapped[str | None] = mapped_column(String, nullable=True)
    sources: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class ConversationSummary(Base):
    __tablename__ = "conversation_summaries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    message_range_start: Mapped[int] = mapped_column(Integer, nullable=False)
    message_range_end: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class HitlRequest(Base):
    __tablename__ = "hitl_requests"
    __table_args__ = (
        Index("ix_hitl_conversation_status", "conversation_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    tool_names: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[HitlStatusEnum] = mapped_column(
        Enum(HitlStatusEnum, create_type=False), default=HitlStatusEnum.pending, nullable=False
    )
    user_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class MessageArtifact(Base):
    __tablename__ = "message_artifacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class UserLongTermMemory(Base):
    __tablename__ = "user_long_term_memory"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    critical_facts: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    preferences: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class ChatModelService:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def create_conversation(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> Conversation:
        conv = Conversation(workspace_id=workspace_id, user_id=user_id)
        self._db.add(conv)
        await self._db.flush()
        return conv

    async def list_conversations(
        self,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
        limit: int,
        cursor_created_at: datetime | None,
        cursor_id: uuid.UUID | None,
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
        return list(await self._db.scalars(query))

    async def get_conversation(self, conversation_id: uuid.UUID) -> Conversation | None:
        return await self._db.get(Conversation, conversation_id)

    async def get_user_conversation(self, conversation_id: uuid.UUID, user_id: uuid.UUID) -> Conversation:
        conv = await self.get_conversation(conversation_id)
        if not conv:
            raise NotFoundError("Conversation", str(conversation_id))
        if conv.user_id != user_id:
            raise ForbiddenError("Access denied")
        return conv

    async def delete_conversation(self, conversation_id: uuid.UUID, user_id: uuid.UUID) -> None:
        conv = await self.get_user_conversation(conversation_id, user_id)
        await self._db.delete(conv)

    async def get_messages(
        self,
        conversation_id: uuid.UUID,
        cursor_created_at: datetime | None,
        cursor_id: uuid.UUID | None,
        limit: int,
    ) -> tuple[list[Message], bool]:
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
        conversation_id: uuid.UUID,
        role: MessageRoleEnum,
        content: str,
        token_details: dict | None,
        total_cost_usd: float | None,
        latency_ms: int | None,
        langfuse_trace_id: str | None,
        sources: list[dict] | None,
    ) -> Message:
        msg = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            token_details=token_details,
            total_cost_usd=total_cost_usd,
            latency_ms=latency_ms,
            langfuse_trace_id=langfuse_trace_id,
            sources=sources,
        )
        self._db.add(msg)
        await self._db.flush()
        return msg

    async def load_memory_context(
        self,
        conversation_id: uuid.UUID,
        short_term_window: int,
    ) -> tuple[list[str], list[dict]]:
        summaries_result = list(await self._db.scalars(
            select(ConversationSummary)
            .where(ConversationSummary.conversation_id == conversation_id)
            .order_by(ConversationSummary.created_at.asc())
        ))
        summaries = [s.summary for s in summaries_result]
        summarized_up_to = max((s.message_range_end for s in summaries_result), default=0)
        total_count = await self._db.scalar(
            select(func.count(Message.id)).where(Message.conversation_id == conversation_id)
        ) or 0
        offset = max(summarized_up_to, total_count - short_term_window)
        msgs_result = await self._db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc())
            .offset(offset)
            .limit(short_term_window)
        )
        recent = [{"role": m.role.value, "content": m.content} for m in msgs_result]
        return summaries, recent

    async def save_summary(
        self,
        conversation_id: uuid.UUID,
        summary: str,
        compress_first_n: int,
    ) -> None:
        max_end = await self._db.scalar(
            select(func.max(ConversationSummary.message_range_end))
            .where(ConversationSummary.conversation_id == conversation_id)
        ) or 0
        self._db.add(
            ConversationSummary(
                conversation_id=conversation_id,
                summary=summary,
                message_range_start=max_end,
                message_range_end=max_end + compress_first_n,
            )
        )

    async def get_long_term_memory(self, user_id: uuid.UUID) -> LongTermMemory:
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
        self,
        user_id: uuid.UUID,
        critical_facts: dict,
        preferences: dict,
    ) -> None:
        record = await self._db.scalar(
            select(UserLongTermMemory).where(UserLongTermMemory.user_id == user_id)
        )
        if record:
            merged_facts = dict(record.critical_facts)
            for key, value in critical_facts.items():
                if value not in (None, "", [], {}):
                    merged_facts[key] = value
            merged_prefs = dict(record.preferences)
            for key, value in preferences.items():
                if value not in (None, "", [], {}):
                    merged_prefs[key] = value
            record.critical_facts = merged_facts
            record.preferences = merged_prefs
            record.updated_at = datetime.now(timezone.utc)
            return
        self._db.add(
            UserLongTermMemory(user_id=user_id, critical_facts=critical_facts, preferences=preferences)
        )

    async def create_hitl_request(
        self,
        conversation_id: uuid.UUID,
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
        request_id: uuid.UUID,
        user_id: uuid.UUID,
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

    async def update_conversation_title(self, conversation_id: uuid.UUID, title: str) -> None:
        conv = await self._db.get(Conversation, conversation_id)
        if conv:
            conv.title = title
            conv.updated_at = datetime.now(timezone.utc)

    async def save_artifact(
        self,
        message_id: uuid.UUID,
        conversation_id: uuid.UUID,
        user_id: uuid.UUID,
        artifact_type: str,
        title: str,
        filename: str,
        storage_key: str,
        content: str | None,
    ) -> MessageArtifact:
        artifact = MessageArtifact(
            message_id=message_id,
            conversation_id=conversation_id,
            user_id=user_id,
            type=artifact_type,
            title=title,
            filename=filename,
            storage_key=storage_key,
            content=content,
        )
        self._db.add(artifact)
        await self._db.flush()
        return artifact

    async def get_or_create_embed_conversation(
        self,
        conversation_id: uuid.UUID | None,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> uuid.UUID:
        if conversation_id is None:
            conv = await self.create_conversation(workspace_id, user_id)
            return conv.id
        conv = await self._db.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.workspace_id == workspace_id,
            )
        )
        if conv:
            return conv.id
        new_conv = await self.create_conversation(workspace_id, user_id)
        return new_conv.id

    async def get_message_usage(self, message_id: uuid.UUID) -> tuple[float, int]:
        msg = await self._db.get(Message, message_id)
        if not msg:
            return 0.0, 0
        return msg.total_cost_usd or 0.0, (msg.token_details or {}).get("total_tokens", 0)

    async def get_artifact_by_message_and_type(
        self,
        message_id: uuid.UUID,
        artifact_type: str,
    ) -> MessageArtifact | None:
        return await self._db.scalar(
            select(MessageArtifact)
            .where(MessageArtifact.message_id == message_id, MessageArtifact.type == artifact_type)
        )

    async def get_artifacts_for_messages(
        self,
        message_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, list[MessageArtifact]]:
        if not message_ids:
            return {}
        rows = list(await self._db.scalars(
            select(MessageArtifact).where(MessageArtifact.message_id.in_(message_ids))
        ))
        result: dict[uuid.UUID, list[MessageArtifact]] = {}
        for row in rows:
            result.setdefault(row.message_id, []).append(row)
        return result

    async def list_workspace_agents(self, workspace_id: uuid.UUID) -> list:
        from app.agents.db_models import Agent

        return list(await self._db.scalars(
            select(Agent).where(
                and_(Agent.workspace_id == workspace_id, Agent.deleted_at.is_(None))
            )
        ))

    async def list_agent_knowledge_base_links(self, agent_ids: list[uuid.UUID]) -> list:
        if not agent_ids:
            return []
        from app.knowledge_bases.db_models import AgentKnowledgeBase

        return list(await self._db.scalars(
            select(AgentKnowledgeBase).where(AgentKnowledgeBase.agent_id.in_(agent_ids))
        ))

    async def list_agent_website_collection_links(self, agent_ids: list[uuid.UUID]) -> list:
        if not agent_ids:
            return []
        from app.website_collections.db_models import AgentWebsiteCollection

        return list(await self._db.scalars(
            select(AgentWebsiteCollection).where(AgentWebsiteCollection.agent_id.in_(agent_ids))
        ))

    async def list_knowledge_bases_by_ids(self, kb_ids: list[uuid.UUID]) -> list:
        if not kb_ids:
            return []
        from app.knowledge_bases.db_models import KnowledgeBase

        return list(await self._db.scalars(select(KnowledgeBase).where(KnowledgeBase.id.in_(kb_ids))))

    async def list_website_collections_by_ids(self, collection_ids: list[uuid.UUID]) -> list:
        if not collection_ids:
            return []
        from app.website_collections.db_models import WebsiteCollection

        return list(await self._db.scalars(
            select(WebsiteCollection).where(WebsiteCollection.id.in_(collection_ids))
        ))

    async def list_connector_instances_for_user(self, user_id: uuid.UUID) -> list:
        from app.connectors.db_models import ConnectorInstance

        return list(await self._db.scalars(
            select(ConnectorInstance)
            .where(ConnectorInstance.user_id == user_id)
            .options(selectinload(ConnectorInstance.definition))
        ))

    async def get_composer_agent(self, workspace_id: uuid.UUID):
        from app.agents.db_models import Agent, AgentTypeEnum

        return await self._db.scalar(
            select(Agent).where(and_(
                Agent.workspace_id == workspace_id,
                Agent.agent_type == AgentTypeEnum.COMPOSER,
                Agent.deleted_at.is_(None),
            ))
        )

    async def list_active_mcp_servers(self, user_id: uuid.UUID) -> list:
        from app.mcp_servers.db_models import MCPServer

        return list(await self._db.scalars(
            select(MCPServer).where(MCPServer.user_id == user_id, MCPServer.is_active.is_(True))
        ))

    async def get_persona_prompt(self, persona_id: uuid.UUID, user_id: uuid.UUID) -> str | None:
        from app.personas.db_models import Persona

        persona = await self._db.get(Persona, persona_id)
        if not persona or persona.user_id != user_id:
            return None
        return persona.system_prompt

    async def save_changes(self) -> None:
        await self._db.commit()
