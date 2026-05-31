import logging
import secrets
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.workspaces.db_models import Workspace
from app.workspaces.models import WorkspaceEmbedResponse

if TYPE_CHECKING:
    from app.auth.db_models import User

logger = logging.getLogger(__name__)


class WorkspaceManager:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def list_workspaces(
        self,
        user_id: UUID,
        limit: int = 20,
        cursor_created_at: datetime | None = None,
        cursor_id: UUID | None = None,
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
                | (
                    (Workspace.created_at == cursor_created_at)
                    & (Workspace.id < cursor_id)
                )
            )
        result = await self._db.scalars(query)
        return list(result)

    async def find_by_name(self, user_id: UUID, name: str) -> Workspace | None:
        return await self._db.scalar(
            select(Workspace).where(
                and_(Workspace.user_id == user_id, Workspace.name == name, Workspace.deleted_at.is_(None))
            )
        )

    async def create_workspace(self, user_id: UUID, name: str, description: str | None) -> Workspace:
        workspace = Workspace(user_id=user_id, name=name, description=description)
        self._db.add(workspace)
        try:
            await self._db.flush()
        except IntegrityError as e:
            await self._db.rollback()
            raise ConflictError(f'A workspace named "{name}" already exists.') from e
        await self._create_system_agents(workspace)
        return workspace

    async def get_workspace(self, workspace_id: UUID, user_id: UUID) -> Workspace:
        ws = await self._db.get(Workspace, workspace_id)
        if not ws or ws.deleted_at:
            raise NotFoundError("Workspace", str(workspace_id))
        if ws.user_id != user_id:
            raise ForbiddenError("Access denied")
        return ws

    async def update_workspace(
        self, workspace_id: UUID, user_id: UUID, name: str | None, description: str | None
    ) -> Workspace:
        ws = await self.get_workspace(workspace_id, user_id)
        if name is not None:
            ws.name = name
        if description is not None:
            ws.description = description
        ws.updated_at = datetime.now(timezone.utc)
        return ws

    async def delete_workspace(self, workspace_id: UUID, user_id: UUID) -> None:
        from app.agents.db_models import Agent
        from app.chat.db_models import Conversation

        ws = await self.get_workspace(workspace_id, user_id)
        ws.deleted_at = datetime.now(timezone.utc)

        # Soft-delete agents via bulk UPDATE (avoids lazy-load MissingGreenlet)
        await self._db.execute(
            update(Agent)
            .where(and_(Agent.workspace_id == workspace_id, Agent.deleted_at.is_(None)))
            .values(deleted_at=datetime.now(timezone.utc))
        )

        # Hard-delete conversations (plan spec: cascade on soft-delete)
        await self._db.execute(
            delete(Conversation).where(Conversation.workspace_id == workspace_id)
        )

    # -----------------------------------------------------------------------
    # Embed
    # -----------------------------------------------------------------------

    async def enable_embed(self, workspace_id: UUID, user_id: UUID, base_url: str) -> WorkspaceEmbedResponse:
        ws = await self.get_workspace(workspace_id, user_id)
        if not ws.embed_token:
            ws.embed_token = secrets.token_urlsafe(32)
        ws.embed_enabled = True
        ws.updated_at = datetime.now(timezone.utc)
        await self._db.commit()
        url = f"{base_url}/embed/{ws.embed_token}"
        snippet = f'<iframe src="{url}" width="400" height="600" frameborder="0" allow="clipboard-write"></iframe>'
        return self._build_embed_response(ws, embed_url=url, snippet=snippet)

    async def disable_embed(self, workspace_id: UUID, user_id: UUID) -> WorkspaceEmbedResponse:
        ws = await self.get_workspace(workspace_id, user_id)
        ws.embed_enabled = False
        ws.updated_at = datetime.now(timezone.utc)
        await self._db.commit()
        return self._build_embed_response(ws)

    async def update_embed_settings(
        self,
        workspace_id: UUID,
        user_id: UUID,
        hitl_auto_approve: bool | None,
        embed_budget_usd: float | None,
        embed_budget_tokens: int | None,
    ) -> WorkspaceEmbedResponse:
        ws = await self.get_workspace(workspace_id, user_id)
        if hitl_auto_approve is not None:
            ws.embed_hitl_auto_approve = hitl_auto_approve
        if embed_budget_usd is not None:
            ws.embed_budget_usd = embed_budget_usd if embed_budget_usd > 0 else None
        if embed_budget_tokens is not None:
            ws.embed_budget_tokens = embed_budget_tokens if embed_budget_tokens > 0 else None
        ws.updated_at = datetime.now(timezone.utc)
        await self._db.commit()
        return self._build_embed_response(ws)

    async def get_workspace_by_embed_token(self, token: str) -> tuple[Workspace, "User"]:
        from app.auth.db_models import User
        ws = await self._db.scalar(
            select(Workspace).where(Workspace.embed_token == token, Workspace.embed_enabled.is_(True))
        )
        if not ws:
            raise NotFoundError("Embed", token)
        user = await self._db.get(User, ws.user_id)
        if not user:
            raise NotFoundError("User", str(ws.user_id))
        return ws, user

    async def auto_disable_embed(self, workspace_id: UUID) -> None:
        await self._db.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(embed_enabled=False)
        )
        await self._db.commit()

    async def increment_embed_spend(self, workspace_id: UUID, cost_usd: float, tokens: int) -> None:
        await self._db.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(
                embed_spend_usd=Workspace.embed_spend_usd + cost_usd,
                embed_spend_tokens=Workspace.embed_spend_tokens + tokens,
            )
        )
        await self._db.commit()

    async def get_workspace_stats(self, workspace_id: UUID, user_id: UUID) -> dict:
        from app.chat.db_models import Conversation, Message
        await self.get_workspace(workspace_id, user_id)

        conv_count = await self._db.scalar(
            select(func.count(Conversation.id)).where(Conversation.workspace_id == workspace_id)
        ) or 0

        msg_result = await self._db.execute(
            select(
                func.count(Message.id),
                func.coalesce(func.sum(Message.total_cost_usd), 0),
            ).join(Conversation, Message.conversation_id == Conversation.id)
            .where(Conversation.workspace_id == workspace_id)
        )
        msg_row = msg_result.one()
        msg_count = msg_row[0] or 0
        total_cost = float(msg_row[1] or 0)

        return {
            "conversation_count": conv_count,
            "message_count": msg_count,
            "total_cost_usd": round(total_cost, 6),
        }

    async def list_workspace_conversations(self, workspace_id: UUID, user_id: UUID, limit: int = 50, offset: int = 0) -> list[dict]:
        from app.chat.db_models import Conversation, Message
        await self.get_workspace(workspace_id, user_id)

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

    # -----------------------------------------------------------------------
    # Private
    # -----------------------------------------------------------------------

    @staticmethod
    def _build_embed_response(
        ws: Workspace,
        embed_url: str | None = None,
        snippet: str | None = None,
    ) -> WorkspaceEmbedResponse:
        return WorkspaceEmbedResponse(
            embed_enabled=ws.embed_enabled,
            embed_token=ws.embed_token,
            embed_hitl_auto_approve=ws.embed_hitl_auto_approve,
            embed_budget_usd=ws.embed_budget_usd,
            embed_budget_tokens=ws.embed_budget_tokens,
            embed_spend_usd=ws.embed_spend_usd,
            embed_spend_tokens=ws.embed_spend_tokens,
            embed_url=embed_url,
            snippet=snippet,
        )

    async def _create_system_agents(self, workspace: Workspace) -> None:
        from app.agents.db_models import Agent, AgentTypeEnum
        for agent_type, name, order in [
            (AgentTypeEnum.MASTER, "Master Agent", 0),
            (AgentTypeEnum.COMPOSER, "Composer Agent", 9999),
        ]:
            agent = Agent(
                workspace_id=workspace.id,
                user_id=workspace.user_id,
                name=name,
                agent_type=agent_type,
                display_order=order,
                is_editable=False,
            )
            self._db.add(agent)
