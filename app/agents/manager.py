import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.db_models import Agent, AgentModelService, AgentTypeEnum
from app.agents.models import PromptGenerateResponse
from app.common.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.common.langfuse_client import get_compiled_prompt
from app.common.retry import acompletion_with_retry
from app.connectors.manager import ConnectorManager
from config.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


class AgentManager:
    def __init__(self, db: AsyncSession):
        self._db = db
        self._agent_model_service = AgentModelService(db)

    async def list_agents(self, workspace_id: UUID, user_id: UUID) -> list[Agent]:
        return await self._agent_model_service.list_agents(workspace_id, user_id)

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

        try:
            agent = await self._agent_model_service.create_agent(
                workspace_id=workspace_id,
                user_id=user_id,
                name=name,
                system_prompt=system_prompt,
                model_id=model_id,
                api_key_id=api_key_id,
                display_order=display_order,
                tools_config=tools_config,
                agent_type=AgentTypeEnum.CUSTOM,
            )
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
            return await self._agent_model_service.update_agent_fields(agent, **allowed)

        try:
            await self._agent_model_service.update_agent_fields(agent, **kwargs)
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
        rows = await self._agent_model_service.list_agent_website_collection_links(agent_ids)
        result: dict[UUID, list[UUID]] = {aid: [] for aid in agent_ids}
        for row in rows:
            result[row.agent_id].append(row.collection_id)
        return result

    async def get_kb_ids_for_agents(self, agent_ids: list[UUID]) -> dict[UUID, list[UUID]]:
        rows = await self._agent_model_service.list_agent_knowledge_base_links(agent_ids)
        result: dict[UUID, list[UUID]] = {aid: [] for aid in agent_ids}
        for row in rows:
            result[row.agent_id].append(row.kb_id)
        return result

    async def delete_agent(self, agent_id: UUID, user_id: UUID) -> None:
        agent = await self._get_editable_agent(agent_id, user_id)
        await self._agent_model_service.soft_delete_agent(agent, datetime.now(timezone.utc))

    async def generate_prompt(
        self,
        workspace_id: UUID,
        user_id: UUID,
        user_description: str,
        api_key_id: UUID,
    ) -> PromptGenerateResponse:
        from app.api_keys.manager import ApiKeyManager

        connector_mgr = ConnectorManager(self._db)
        definitions = await connector_mgr.list_definitions()
        instances = await connector_mgr.list_user_instances(user_id)
        connected_slugs = {inst.definition.slug for inst in instances}

        tools_lines = []
        for defn in definitions:
            status = "connected" if defn.slug in connected_slugs else "not connected"
            for tool_def in defn.tools:
                tools_lines.append(
                    f"- {defn.slug}.{tool_def['name']}: {tool_def.get('description', '')} [{status}]"
                )
        tools_context = "\n".join(tools_lines) if tools_lines else "No tools available."

        from app.connectors.encryption import decrypt_str
        key_mgr = ApiKeyManager(self._db)
        key_rec = await key_mgr._get_key(api_key_id, user_id)
        raw_key = decrypt_str(key_rec.encrypted_key)
        model_id = key_rec.available_models[0] if key_rec.available_models else settings.DEFAULT_MODEL

        prompt_text = get_compiled_prompt("agent_prompt_generator", {
            "user_description": user_description,
            "available_tools": tools_context,
        })

        response = await acompletion_with_retry(
            model=model_id,
            messages=[{"role": "user", "content": prompt_text}],
            response_format={"type": "json_object"},
            api_key=raw_key,
            metadata={"trace_name": "agent_prompt_generator"},
        )

        return PromptGenerateResponse.model_validate_json(response.choices[0].message.content)

    async def _get_agent_for_owner(self, agent_id: UUID, user_id: UUID) -> Agent:
        agent = await self._agent_model_service.get_agent(agent_id)
        if not agent or agent.deleted_at:
            raise NotFoundError("Agent", str(agent_id))
        if agent.user_id != user_id:
            raise ForbiddenError("Access denied")
        return agent

    async def _get_editable_agent(self, agent_id: UUID, user_id: UUID) -> Agent:
        agent = await self._agent_model_service.get_agent(agent_id)
        if not agent or agent.deleted_at:
            raise NotFoundError("Agent", str(agent_id))
        if agent.user_id != user_id:
            raise ForbiddenError("Access denied")
        if not agent.is_editable:
            raise ForbiddenError("Cannot modify system agent")
        return agent
