import logging
import secrets
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.workspaces.db_models import Workspace, WorkspaceModelService
from app.workspaces.models import WorkspaceEmbedResponse

if TYPE_CHECKING:
    from app.auth.db_models import User

logger = logging.getLogger(__name__)


class WorkspaceManager:
    def __init__(self, db: AsyncSession):
        self._db = db
        self._workspace_model_service = WorkspaceModelService(db)

    async def list_workspaces(
        self,
        user_id: UUID,
        limit: int = 20,
        cursor_created_at: datetime | None = None,
        cursor_id: UUID | None = None,
    ) -> list[Workspace]:
        return await self._workspace_model_service.list_workspaces(
            user_id, limit, cursor_created_at, cursor_id
        )

    async def find_by_name(self, user_id: UUID, name: str) -> Workspace | None:
        return await self._workspace_model_service.find_by_name(user_id, name)

    async def create_workspace(self, user_id: UUID, name: str, description: str, workspace_type: str = "standard") -> Workspace:
        try:
            workspace = await self._workspace_model_service.create_workspace(
                user_id, name, description, workspace_type
            )
        except IntegrityError as e:
            await self._workspace_model_service.undo_changes()
            raise ConflictError(f'A workspace named "{name}" already exists.') from e
        await self._create_system_agents(workspace)
        return workspace

    async def get_workspace(self, workspace_id: UUID, user_id: UUID) -> Workspace:
        ws = await self._workspace_model_service.get_workspace(workspace_id)
        if not ws or ws.deleted_at:
            raise NotFoundError("Workspace", str(workspace_id))
        if ws.user_id != user_id:
            raise ForbiddenError("Access denied")
        return ws

    async def update_workspace(
        self, workspace_id: UUID, user_id: UUID, name: str | None, description: str | None
    ) -> Workspace:
        ws = await self.get_workspace(workspace_id, user_id)
        return await self._workspace_model_service.update_workspace_fields(ws, name, description)

    async def delete_workspace(self, workspace_id: UUID, user_id: UUID) -> None:
        from app.agents.db_models import Agent
        from app.chat.db_models import Conversation

        ws = await self.get_workspace(workspace_id, user_id)
        await self._workspace_model_service.soft_delete_workspace(ws)

    # -----------------------------------------------------------------------
    # Embed
    # -----------------------------------------------------------------------

    async def enable_embed(self, workspace_id: UUID, user_id: UUID, base_url: str) -> WorkspaceEmbedResponse:
        ws = await self.get_workspace(workspace_id, user_id)
        embed_token = ws.embed_token or secrets.token_urlsafe(32)
        await self._workspace_model_service.set_embed_enabled(ws, True, embed_token=embed_token)
        await self._workspace_model_service.save_changes()
        url = f"{base_url}/embed/{ws.embed_token}"
        snippet = f'<iframe src="{url}" width="400" height="600" frameborder="0" allow="clipboard-write"></iframe>'
        return self._build_embed_response(ws, embed_url=url, snippet=snippet)

    async def disable_embed(self, workspace_id: UUID, user_id: UUID) -> WorkspaceEmbedResponse:
        ws = await self.get_workspace(workspace_id, user_id)
        await self._workspace_model_service.set_embed_enabled(ws, False)
        await self._workspace_model_service.save_changes()
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
        await self._workspace_model_service.update_embed_settings(
            ws, hitl_auto_approve, embed_budget_usd, embed_budget_tokens
        )
        await self._workspace_model_service.save_changes()
        return self._build_embed_response(ws)

    async def get_workspace_by_embed_token(self, token: str) -> tuple[Workspace, "User"]:
        ws = await self._workspace_model_service.get_workspace_by_embed_token(token)
        if not ws:
            raise NotFoundError("Embed", token)
        user = await self._workspace_model_service.get_user_by_id(ws.user_id)
        if not user:
            raise NotFoundError("User", str(ws.user_id))
        return ws, user

    async def auto_disable_embed(self, workspace_id: UUID) -> None:
        await self._workspace_model_service.auto_disable_embed(workspace_id)
        await self._workspace_model_service.save_changes()

    async def increment_embed_spend(self, workspace_id: UUID, cost_usd: float, tokens: int) -> None:
        await self._workspace_model_service.increment_embed_spend(workspace_id, cost_usd, tokens)
        await self._workspace_model_service.save_changes()

    async def get_workspace_stats(self, workspace_id: UUID, user_id: UUID) -> dict:
        await self.get_workspace(workspace_id, user_id)
        return await self._workspace_model_service.get_workspace_stats(workspace_id)

    async def list_workspace_conversations(self, workspace_id: UUID, user_id: UUID, limit: int = 50, offset: int = 0) -> list[dict]:
        await self.get_workspace(workspace_id, user_id)
        return await self._workspace_model_service.list_workspace_conversations(
            workspace_id, limit, offset
        )

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
        from app.api_keys.manager import ApiKeyManager
        from config.settings import get_settings

        _settings = get_settings()
        keys = await ApiKeyManager(self._db).list_keys(workspace.user_id)
        default_api_key_id = keys[0].id if keys else None

        for agent_type, name, order in [
            (AgentTypeEnum.MASTER, "Master Agent", 0),
            (AgentTypeEnum.COMPOSER, "Composer Agent", 9999),
        ]:
            await self._workspace_model_service.add_system_agent(
                workspace,
                name=name,
                agent_type=agent_type,
                display_order=order,
                default_model=_settings.DEFAULT_MODEL,
                api_key_id=default_api_key_id,
            )
