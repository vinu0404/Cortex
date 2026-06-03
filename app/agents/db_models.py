import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, select
from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.session import Base


class AgentTypeEnum(str, enum.Enum):
    MASTER = "MASTER"
    CUSTOM = "CUSTOM"
    COMPOSER = "COMPOSER"


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (
        Index(
            "uq_agent_workspace_name", "workspace_id", "name",
            unique=True, postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_agents_workspace_deleted", "workspace_id", "deleted_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_type: Mapped[AgentTypeEnum] = mapped_column(
        Enum(AgentTypeEnum, create_type=False), default=AgentTypeEnum.CUSTOM, nullable=False
    )
    model_id: Mapped[str | None] = mapped_column(String, nullable=True)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_api_keys.id", ondelete="SET NULL"), nullable=True
    )
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_editable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    tools_config: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    workspace: Mapped["Workspace"] = relationship("Workspace", back_populates="agents")
    agent_personas: Mapped[list["AgentPersona"]] = relationship(
        "AgentPersona", back_populates="agent", cascade="all, delete-orphan"
    )


class AgentModelService:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def list_agents(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> list[Agent]:
        result = await self._db.scalars(
            select(Agent)
            .where(and_(
                Agent.workspace_id == workspace_id,
                Agent.user_id == user_id,
                Agent.deleted_at.is_(None),
            ))
            .order_by(Agent.display_order)
        )
        return list(result)

    async def create_agent(
        self,
        *,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
        name: str,
        system_prompt: str | None,
        model_id: str | None,
        api_key_id: uuid.UUID | None,
        display_order: int,
        tools_config: list[dict],
        agent_type: AgentTypeEnum = AgentTypeEnum.CUSTOM,
    ) -> Agent:
        agent = Agent(
            workspace_id=workspace_id,
            user_id=user_id,
            name=name,
            system_prompt=system_prompt,
            agent_type=agent_type,
            model_id=model_id,
            api_key_id=api_key_id,
            display_order=display_order,
            tools_config=tools_config,
        )
        self._db.add(agent)
        await self._db.flush()
        return agent

    async def get_agent(self, agent_id: uuid.UUID) -> Agent | None:
        return await self._db.get(Agent, agent_id)

    async def update_agent_fields(self, agent: Agent, **kwargs) -> Agent:
        for field, value in kwargs.items():
            if value is not None:
                setattr(agent, field, value)
        agent.updated_at = datetime.now(timezone.utc)
        await self._db.flush()
        return agent

    async def list_agent_website_collection_links(self, agent_ids: list[uuid.UUID]) -> list:
        if not agent_ids:
            return []
        from app.website_collections.db_models import AgentWebsiteCollection

        return list(await self._db.scalars(
            select(AgentWebsiteCollection).where(AgentWebsiteCollection.agent_id.in_(agent_ids))
        ))

    async def list_agent_knowledge_base_links(self, agent_ids: list[uuid.UUID]) -> list:
        if not agent_ids:
            return []
        from app.knowledge_bases.db_models import AgentKnowledgeBase

        return list(await self._db.scalars(
            select(AgentKnowledgeBase).where(AgentKnowledgeBase.agent_id.in_(agent_ids))
        ))

    async def soft_delete_agent(self, agent: Agent, deleted_at: datetime) -> None:
        agent.deleted_at = deleted_at
