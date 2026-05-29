import logging
from datetime import datetime, timezone
from uuid import UUID

import litellm
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.db_models import Agent, AgentTypeEnum
from app.agents.models import PromptGenerateResponse
from app.common.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.common.langfuse_client import get_compiled_prompt
from app.connectors.manager import ConnectorManager
from config.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


class _PromptGenOutput(BaseModel):
    generated_prompt: str
    recommended_tools: list[dict]
    recommended_mcp: list = []


class AgentManager:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def list_agents(self, workspace_id: UUID, user_id: UUID) -> list[Agent]:
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
        workspace_id: UUID,
        user_id: UUID,
        name: str,
        system_prompt: str | None,
        model_id: str | None,
        api_key_id: UUID | None,
        display_order: int,
        tools_config: list[dict],
        kb_ids: list[UUID] | None = None,
        collection_ids: list[UUID] | None = None,
    ) -> Agent:
        from app.workspaces.manager import WorkspaceManager
        await WorkspaceManager(self._db).get_workspace(workspace_id, user_id)

        agent = Agent(
            workspace_id=workspace_id,
            user_id=user_id,
            name=name,
            system_prompt=system_prompt,
            agent_type=AgentTypeEnum.CUSTOM,
            model_id=model_id,
            api_key_id=api_key_id,
            display_order=display_order,
            tools_config=tools_config,
        )
        self._db.add(agent)
        try:
            await self._db.flush()
        except IntegrityError as e:
            raise ConflictError(f"Agent name '{name}' already exists in this workspace") from e

        if kb_ids:
            from app.knowledge_bases.manager import KnowledgeBaseManager
            await KnowledgeBaseManager(self._db).set_agent_kbs(agent.id, kb_ids)

        if collection_ids:
            from app.website_collections.manager import WebsiteCollectionManager
            await WebsiteCollectionManager(self._db).set_agent_website_collections(agent.id, collection_ids)

        return agent

    async def update_agent(self, agent_id: UUID, user_id: UUID, **kwargs) -> Agent:
        kb_ids: list[UUID] | None = kwargs.pop("kb_ids", None)
        collection_ids: list[UUID] | None = kwargs.pop("collection_ids", None)

        agent = await self._get_agent_for_owner(agent_id, user_id)
        if not agent.is_editable:
            allowed = {k: v for k, v in kwargs.items() if k in ("model_id", "api_key_id")}
            if not allowed or kb_ids is not None or collection_ids is not None:
                raise ForbiddenError("Cannot modify system agent")
            for field, value in allowed.items():
                if value is not None:
                    setattr(agent, field, value)
            agent.updated_at = datetime.now(timezone.utc)
            await self._db.flush()
            return agent

        for field, value in kwargs.items():
            if value is not None:
                setattr(agent, field, value)
        agent.updated_at = datetime.now(timezone.utc)
        try:
            await self._db.flush()
        except IntegrityError as e:
            raise ConflictError("Agent name already exists in this workspace") from e

        if kb_ids is not None:
            from app.knowledge_bases.manager import KnowledgeBaseManager
            await KnowledgeBaseManager(self._db).set_agent_kbs(agent.id, kb_ids)

        if collection_ids is not None:
            from app.website_collections.manager import WebsiteCollectionManager
            await WebsiteCollectionManager(self._db).set_agent_website_collections(agent.id, collection_ids)

        return agent

    async def get_collection_ids_for_agents(self, agent_ids: list[UUID]) -> dict[UUID, list[UUID]]:
        if not agent_ids:
            return {}
        from app.website_collections.db_models import AgentWebsiteCollection
        rows = list(await self._db.scalars(
            select(AgentWebsiteCollection).where(AgentWebsiteCollection.agent_id.in_(agent_ids))
        ))
        result: dict[UUID, list[UUID]] = {aid: [] for aid in agent_ids}
        for row in rows:
            result[row.agent_id].append(row.collection_id)
        return result

    async def get_kb_ids_for_agents(self, agent_ids: list[UUID]) -> dict[UUID, list[UUID]]:
        if not agent_ids:
            return {}
        from app.knowledge_bases.db_models import AgentKnowledgeBase
        rows = list(await self._db.scalars(
            select(AgentKnowledgeBase).where(AgentKnowledgeBase.agent_id.in_(agent_ids))
        ))
        result: dict[UUID, list[UUID]] = {aid: [] for aid in agent_ids}
        for row in rows:
            result[row.agent_id].append(row.kb_id)
        return result

    async def delete_agent(self, agent_id: UUID, user_id: UUID) -> None:
        agent = await self._get_editable_agent(agent_id, user_id)
        agent.deleted_at = datetime.now(timezone.utc)

    async def generate_prompt(
        self,
        workspace_id: UUID,
        user_id: UUID,
        user_description: str,
        api_key_id: UUID,
    ) -> PromptGenerateResponse:
        from app.api_keys.manager import ApiKeyManager

        connector_mgr = ConnectorManager(self._db)
        instances = await connector_mgr.list_user_instances(user_id)

        available_tools = []
        for inst in instances:
            for tool_def in inst.definition.tools:
                available_tools.append({
                    "connector_slug": inst.definition.slug,
                    **tool_def,
                })

        tools_json = "\n".join(
            f"- {t['connector_slug']}.{t['name']}: {t.get('description', '')}"
            for t in available_tools
        )

        from app.connectors.encryption import decrypt_str
        key_mgr = ApiKeyManager(self._db)
        key_rec = await key_mgr._get_key(api_key_id, user_id)
        raw_key = decrypt_str(key_rec.encrypted_key)
        model_id = key_rec.available_models[0] if key_rec.available_models else settings.DEFAULT_MODEL

        prompt_text = get_compiled_prompt("agent_prompt_generator", {
            "user_description": user_description,
            "available_tools": tools_json or "No connectors connected yet.",
        })

        response = await litellm.acompletion(
            model=model_id,
            messages=[{"role": "user", "content": prompt_text}],
            response_format=_PromptGenOutput,
            api_key=raw_key,
            metadata={"trace_name": "agent_prompt_generator"},
        )

        result = _PromptGenOutput.model_validate_json(response.choices[0].message.content)
        return PromptGenerateResponse(**result.model_dump())

    async def _get_agent_for_owner(self, agent_id: UUID, user_id: UUID) -> Agent:
        agent = await self._db.get(Agent, agent_id)
        if not agent or agent.deleted_at:
            raise NotFoundError("Agent", str(agent_id))
        if agent.user_id != user_id:
            raise ForbiddenError("Access denied")
        return agent

    async def _get_editable_agent(self, agent_id: UUID, user_id: UUID) -> Agent:
        agent = await self._db.get(Agent, agent_id)
        if not agent or agent.deleted_at:
            raise NotFoundError("Agent", str(agent_id))
        if agent.user_id != user_id:
            raise ForbiddenError("Access denied")
        if not agent.is_editable:
            raise ForbiddenError("Cannot modify system agent")
        return agent
