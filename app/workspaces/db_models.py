import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, String, Text, and_, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.session import Base


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        Index(
            "uq_workspaces_user_name_active",
            "user_id", "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    embed_token: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    embed_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    embed_hitl_auto_approve: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    embed_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    embed_budget_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    embed_spend_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    embed_spend_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    workspace_type: Mapped[str] = mapped_column(String(20), nullable=False, default="standard", server_default="standard")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    agents: Mapped[list] = relationship(
        "Agent", back_populates="workspace", cascade="all, delete-orphan"
    )


class WorkspaceModelService:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def list_workspaces(
        self,
        user_id: uuid.UUID,
        limit: int,
        cursor_created_at: datetime | None,
        cursor_id: uuid.UUID | None,
    ) -> list[Workspace]:
        query = (
            select(Workspace)
            .where(and_(Workspace.user_id == user_id, Workspace.deleted_at.is_(None)))
            .order_by(Workspace.created_at.desc(), Workspace.id.desc())
            .limit(limit + 1)
        )
        if cursor_created_at and cursor_id:
            query = query.where(
                (Workspace.created_at < cursor_created_at)
                | ((Workspace.created_at == cursor_created_at) & (Workspace.id < cursor_id))
            )
        return list(await self._db.scalars(query))

    async def find_by_name(self, user_id: uuid.UUID, name: str) -> Workspace | None:
        return await self._db.scalar(
            select(Workspace).where(
                and_(Workspace.user_id == user_id, Workspace.name == name, Workspace.deleted_at.is_(None))
            )
        )

    async def create_workspace(
        self,
        user_id: uuid.UUID,
        name: str,
        description: str,
        workspace_type: str,
    ) -> Workspace:
        workspace = Workspace(
            user_id=user_id,
            name=name,
            description=description,
            workspace_type=workspace_type,
        )
        self._db.add(workspace)
        await self._db.flush()
        return workspace

    async def get_workspace(self, workspace_id: uuid.UUID) -> Workspace | None:
        return await self._db.get(Workspace, workspace_id)

    async def update_workspace_fields(
        self,
        workspace: Workspace,
        name: str | None,
        description: str | None,
    ) -> Workspace:
        if name is not None:
            workspace.name = name
        if description is not None:
            workspace.description = description
        workspace.updated_at = datetime.now(timezone.utc)
        return workspace

    async def soft_delete_workspace(self, workspace: Workspace) -> None:
        from app.agents.db_models import Agent
        from app.chat.db_models import Conversation

        workspace.deleted_at = datetime.now(timezone.utc)
        await self._db.execute(
            update(Agent)
            .where(and_(Agent.workspace_id == workspace.id, Agent.deleted_at.is_(None)))
            .values(deleted_at=datetime.now(timezone.utc))
        )
        await self._db.execute(
            delete(Conversation).where(Conversation.workspace_id == workspace.id)
        )

    async def set_embed_enabled(
        self,
        workspace: Workspace,
        enabled: bool,
        embed_token: str | None = None,
    ) -> Workspace:
        if embed_token is not None:
            workspace.embed_token = embed_token
        workspace.embed_enabled = enabled
        workspace.updated_at = datetime.now(timezone.utc)
        return workspace

    async def update_embed_settings(
        self,
        workspace: Workspace,
        hitl_auto_approve: bool | None,
        embed_budget_usd: float | None,
        embed_budget_tokens: int | None,
    ) -> Workspace:
        if hitl_auto_approve is not None:
            workspace.embed_hitl_auto_approve = hitl_auto_approve
        if embed_budget_usd is not None:
            workspace.embed_budget_usd = embed_budget_usd if embed_budget_usd > 0 else None
        if embed_budget_tokens is not None:
            workspace.embed_budget_tokens = embed_budget_tokens if embed_budget_tokens > 0 else None
        workspace.updated_at = datetime.now(timezone.utc)
        return workspace

    async def get_workspace_by_embed_token(self, token: str) -> Workspace | None:
        return await self._db.scalar(
            select(Workspace).where(Workspace.embed_token == token, Workspace.embed_enabled.is_(True))
        )

    async def get_user_by_id(self, user_id: uuid.UUID):
        from app.auth.db_models import User

        return await self._db.get(User, user_id)

    async def auto_disable_embed(self, workspace_id: uuid.UUID) -> None:
        await self._db.execute(
            update(Workspace).where(Workspace.id == workspace_id).values(embed_enabled=False)
        )

    async def increment_embed_spend(self, workspace_id: uuid.UUID, cost_usd: float, tokens: int) -> None:
        await self._db.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(
                embed_spend_usd=Workspace.embed_spend_usd + cost_usd,
                embed_spend_tokens=Workspace.embed_spend_tokens + tokens,
            )
        )

    async def get_workspace_stats(self, workspace_id: uuid.UUID) -> dict:
        from app.chat.db_models import Conversation, Message

        conv_count = await self._db.scalar(
            select(func.count(Conversation.id)).where(Conversation.workspace_id == workspace_id)
        ) or 0
        msg_row = (
            await self._db.execute(
                select(
                    func.count(Message.id),
                    func.coalesce(func.sum(Message.total_cost_usd), 0),
                )
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(Conversation.workspace_id == workspace_id)
            )
        ).one()
        return {
            "conversation_count": conv_count,
            "message_count": msg_row[0] or 0,
            "total_cost_usd": round(float(msg_row[1] or 0), 6),
        }

    async def list_workspace_conversations(self, workspace_id: uuid.UUID, limit: int, offset: int) -> list[dict]:
        from app.chat.db_models import Conversation, Message

        rows = await self._db.execute(
            select(
                Conversation.id,
                Conversation.title,
                Conversation.created_at,
                func.count(Message.id).label("message_count"),
                func.coalesce(func.sum(Message.total_cost_usd), 0).label("total_cost"),
            )
            .outerjoin(Message, Message.conversation_id == Conversation.id)
            .where(Conversation.workspace_id == workspace_id)
            .group_by(Conversation.id, Conversation.title, Conversation.created_at)
            .order_by(Conversation.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [
            {
                "id": str(row.id),
                "title": row.title or "Untitled",
                "created_at": row.created_at.isoformat(),
                "message_count": row.message_count or 0,
                "total_cost_usd": round(float(row.total_cost or 0), 6),
            }
            for row in rows
        ]

    async def add_system_agent(
        self,
        workspace: Workspace,
        *,
        name: str,
        agent_type,
        display_order: int,
        default_model: str,
        api_key_id: uuid.UUID | None,
    ) -> None:
        from app.agents.db_models import Agent

        self._db.add(
            Agent(
                workspace_id=workspace.id,
                user_id=workspace.user_id,
                name=name,
                agent_type=agent_type,
                display_order=display_order,
                is_editable=False,
                model_id=default_model if api_key_id else None,
                api_key_id=api_key_id,
            )
        )

    async def save_changes(self) -> None:
        await self._db.commit()

    async def undo_changes(self) -> None:
        await self._db.rollback()

    async def refresh(self, instance) -> None:
        await self._db.refresh(instance)
