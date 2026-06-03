import enum
import uuid
from collections import defaultdict
from datetime import date, timedelta
from datetime import datetime, timezone
from uuid import UUID as PyUUID

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, and_, distinct, func, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.agents.db_models import Agent
from app.chat.db_models import Conversation, Message
from app.connectors.db_models import ConnectorInstance, ConnectorStatusEnum
from app.knowledge_bases.db_models import KnowledgeBase
from app.personas.db_models import AgentPersona, Persona
from app.website_collections.db_models import WebsiteCollection
from app.workspaces.db_models import Workspace
from database.session import Base


class RoleEnum(str, enum.Enum):
    user = "user"
    admin = "admin"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[RoleEnum] = mapped_column(Enum(RoleEnum, create_type=False), default=RoleEnum.user, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    vinu_agent_name: Mapped[str | None] = mapped_column(String, nullable=True)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False, default="UTC", server_default="UTC")

    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        "RefreshToken", back_populates="user", cascade="all, delete-orphan"
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    user: Mapped["User"] = relationship("User", back_populates="refresh_tokens")


class AuthModelService:
    """DB-layer reads and persistence helpers for auth and dashboard flows."""

    def __init__(self, db: AsyncSession):
        self._db = db

    async def create_user(self, email: str, hashed_password: str) -> User:
        user = User(email=email, hashed_password=hashed_password)
        self._db.add(user)
        await self._db.flush()
        return user

    async def find_user_by_email(self, email: str) -> User | None:
        return await self._db.scalar(select(User).where(User.email == email))

    async def get_user_by_id(self, user_id: PyUUID) -> User | None:
        return await self._db.get(User, user_id)

    async def get_valid_refresh_token(self, token_hash: str, now: datetime) -> RefreshToken | None:
        return await self._db.scalar(
            select(RefreshToken)
            .where(RefreshToken.token_hash == token_hash)
            .where(RefreshToken.revoked_at.is_(None))
            .where(RefreshToken.expires_at > now)
        )

    async def get_active_refresh_token(self, token_hash: str) -> RefreshToken | None:
        return await self._db.scalar(
            select(RefreshToken)
            .where(RefreshToken.token_hash == token_hash)
            .where(RefreshToken.revoked_at.is_(None))
        )

    async def create_refresh_token(self, user_id: PyUUID, token_hash: str, expires_at: datetime) -> RefreshToken:
        record = RefreshToken(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        self._db.add(record)
        await self._db.flush()
        return record

    async def revoke_refresh_token(self, record: RefreshToken, revoked_at: datetime) -> None:
        record.revoked_at = revoked_at
        await self._db.flush()

    async def update_user_password(self, user: User, hashed_password: str) -> User:
        user.hashed_password = hashed_password
        return user

    async def update_user_profile(
        self,
        user: User,
        timezone_value: str | None,
        vinu_agent_name: str | None,
    ) -> User:
        if timezone_value is not None:
            user.timezone = timezone_value
        if vinu_agent_name is not None:
            user.vinu_agent_name = vinu_agent_name
        return user

    async def get_user_stats(self, user_id: PyUUID) -> dict:
        ws_count = await self._db.scalar(
            select(func.count()).select_from(Workspace).where(
                Workspace.user_id == user_id,
                Workspace.deleted_at.is_(None),
            )
        )
        agent_count = await self._db.scalar(
            select(func.count(Agent.id)).join(Workspace, Agent.workspace_id == Workspace.id).where(
                Workspace.user_id == user_id,
                Workspace.deleted_at.is_(None),
            )
        )
        conv_count = await self._db.scalar(
            select(func.count()).select_from(Conversation).where(Conversation.user_id == user_id)
        )
        msg_count = await self._db.scalar(
            select(func.count()).select_from(Message).join(
                Conversation, Message.conversation_id == Conversation.id
            ).where(Conversation.user_id == user_id)
        )
        total_cost = await self._db.scalar(
            select(func.sum(Message.total_cost_usd)).join(
                Conversation, Message.conversation_id == Conversation.id
            ).where(Conversation.user_id == user_id)
        )
        return {
            "workspaces": ws_count or 0,
            "agents": agent_count or 0,
            "conversations": conv_count or 0,
            "messages": msg_count or 0,
            "total_cost_usd": round(total_cost or 0.0, 4),
            "knowledge_bases": await self.count_user_resources(KnowledgeBase, user_id),
            "website_collections": await self.count_user_resources(WebsiteCollection, user_id),
            "active_connectors": await self.count_user_resources(ConnectorInstance, user_id),
        }

    async def get_recent_conversations(self, user_id: PyUUID, limit: int) -> list[dict]:
        rows = await self._db.execute(
            select(
                Conversation.id,
                Conversation.title,
                Conversation.workspace_id,
                Conversation.created_at,
                Workspace.name.label("workspace_name"),
            )
            .join(Workspace, Conversation.workspace_id == Workspace.id)
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.created_at.desc())
            .limit(limit)
        )
        return [
            {
                "id": str(row.id),
                "title": row.title,
                "workspace_id": str(row.workspace_id),
                "workspace_name": row.workspace_name,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]

    async def get_dashboard_analytics(self, user_id: PyUUID, days: int = 15, workspace_limit: int = 6) -> dict:
        totals = await self.get_user_stats(user_id)

        start_day = datetime.now(timezone.utc).date() - timedelta(days=max(days - 1, 0))
        start_dt = datetime.combine(start_day, datetime.min.time(), tzinfo=timezone.utc)

        conv_daily_rows = await self._db.execute(
            select(
                func.date(Conversation.created_at).label("bucket_day"),
                func.count(Conversation.id).label("conversation_count"),
            )
            .where(
                Conversation.user_id == user_id,
                Conversation.created_at >= start_dt,
            )
            .group_by(func.date(Conversation.created_at))
            .order_by(func.date(Conversation.created_at))
        )
        message_daily_rows = await self._db.execute(
            select(
                func.date(Message.created_at).label("bucket_day"),
                func.count(Message.id).label("message_count"),
                func.coalesce(func.sum(Message.total_cost_usd), 0).label("cost_usd"),
            )
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(
                Conversation.user_id == user_id,
                Message.created_at >= start_dt,
            )
            .group_by(func.date(Message.created_at))
            .order_by(func.date(Message.created_at))
        )

        activity_map: dict[date, dict[str, float | int]] = defaultdict(
            lambda: {"conversations": 0, "messages": 0, "cost_usd": 0.0}
        )
        for row in conv_daily_rows:
            activity_map[row.bucket_day]["conversations"] = int(row.conversation_count or 0)
        for row in message_daily_rows:
            activity_map[row.bucket_day]["messages"] = int(row.message_count or 0)
            activity_map[row.bucket_day]["cost_usd"] = round(float(row.cost_usd or 0), 6)

        activity_series = []
        for offset in range(days):
            current_day = start_day + timedelta(days=offset)
            bucket = activity_map[current_day]
            activity_series.append({
                "date": current_day.isoformat(),
                "conversations": int(bucket["conversations"]),
                "messages": int(bucket["messages"]),
                "cost_usd": round(float(bucket["cost_usd"]), 6),
            })

        window_conversations = sum(item["conversations"] for item in activity_series)
        window_messages = sum(item["messages"] for item in activity_series)
        window_cost_usd = round(sum(item["cost_usd"] for item in activity_series), 6)

        workspace_rows = await self._db.execute(
            select(
                Workspace.id.label("workspace_id"),
                Workspace.name.label("workspace_name"),
                func.count(distinct(Conversation.id)).label("conversation_count"),
                func.count(Message.id).label("message_count"),
                func.coalesce(func.sum(Message.total_cost_usd), 0).label("total_cost_usd"),
            )
            .select_from(Workspace)
            .outerjoin(
                Conversation,
                and_(
                    Conversation.workspace_id == Workspace.id,
                    Conversation.created_at >= start_dt,
                ),
            )
            .outerjoin(
                Message,
                and_(
                    Message.conversation_id == Conversation.id,
                    Message.created_at >= start_dt,
                ),
            )
            .where(
                Workspace.user_id == user_id,
                Workspace.deleted_at.is_(None),
            )
            .group_by(Workspace.id, Workspace.name)
            .order_by(
                func.count(Message.id).desc(),
                func.coalesce(func.sum(Message.total_cost_usd), 0).desc(),
                Workspace.created_at.desc(),
            )
            .limit(workspace_limit)
        )
        workspace_breakdown = [
            {
                "workspace_id": str(row.workspace_id),
                "workspace_name": row.workspace_name,
                "conversation_count": int(row.conversation_count or 0),
                "message_count": int(row.message_count or 0),
                "total_cost_usd": round(float(row.total_cost_usd or 0), 6),
            }
            for row in workspace_rows
        ]
        active_window_workspaces = sum(
            1 for row in workspace_breakdown
            if row["conversation_count"] > 0 or row["message_count"] > 0 or row["total_cost_usd"] > 0
        )

        connector_status_rows = await self._db.execute(
            select(
                ConnectorInstance.status,
                func.count(ConnectorInstance.id).label("count"),
            )
            .where(ConnectorInstance.user_id == user_id)
            .group_by(ConnectorInstance.status)
        )
        connector_status_counts = {status.value: 0 for status in ConnectorStatusEnum}
        for row in connector_status_rows:
            connector_status_counts[row.status.value] = int(row.count or 0)
        connector_status_breakdown = [
            {"status": status, "count": count}
            for status, count in connector_status_counts.items()
        ]

        persona_total = await self.count_user_resources(Persona, user_id)
        persona_link_rows = await self._db.execute(
            select(
                func.count(distinct(AgentPersona.persona_id)).label("assigned_personas"),
                func.count(distinct(AgentPersona.agent_id)).label("agents_with_personas"),
                func.count().label("assignment_links"),
            )
            .select_from(Persona)
            .outerjoin(AgentPersona, AgentPersona.persona_id == Persona.id)
            .where(Persona.user_id == user_id)
        )
        persona_link_summary = persona_link_rows.one()

        return {
            "totals": totals,
            "window_summary": {
                "days": days,
                "conversations": window_conversations,
                "messages": window_messages,
                "total_cost_usd": window_cost_usd,
                "active_workspaces": active_window_workspaces,
            },
            "activity_series": activity_series,
            "workspace_breakdown": workspace_breakdown,
            "resource_breakdown": {
                "knowledge_bases": await self.count_user_resources(KnowledgeBase, user_id),
                "website_collections": await self.count_user_resources(WebsiteCollection, user_id),
                "connectors": await self.count_user_resources(ConnectorInstance, user_id),
                "personas": persona_total,
                "agents": await self.count_user_resources(Agent, user_id),
            },
            "connector_status_breakdown": connector_status_breakdown,
            "persona_summary": {
                "total": persona_total,
                "assigned_personas": int(persona_link_summary.assigned_personas or 0),
                "agents_with_personas": int(persona_link_summary.agents_with_personas or 0),
                "assignment_links": int(persona_link_summary.assignment_links or 0),
            },
        }

    async def count_user_resources(self, model, user_id: PyUUID) -> int:
        return await self._db.scalar(
            select(func.count()).select_from(model).where(model.user_id == user_id)
        ) or 0

    async def save_changes(self) -> None:
        await self._db.commit()

    async def refresh(self, instance) -> None:
        await self._db.refresh(instance)
