import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import and_, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import ForbiddenError, NotFoundError
from app.workspaces.db_models import Workspace

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

    async def create_workspace(self, user_id: UUID, name: str, description: str | None) -> Workspace:
        workspace = Workspace(user_id=user_id, name=name, description=description)
        self._db.add(workspace)
        await self._db.flush()
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
