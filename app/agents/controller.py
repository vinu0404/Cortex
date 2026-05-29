import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.manager import AgentManager
from app.agents.models import AgentCreate, AgentResponse, AgentUpdate, PromptGenerateRequest
from app.auth.dependencies import get_current_user
from app.auth.db_models import User
from app.common.api_response import fail, ok
from app.common.exceptions import AppError
from database.session import get_db

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/workspaces/{workspace_id}/agents", response_model=None)
async def list_agents(
    workspace_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = AgentManager(db)
        agents = await manager.list_agents(workspace_id, current_user.id)
        agent_ids = [a.id for a in agents]
        kb_map = await manager.get_kb_ids_for_agents(agent_ids)
        wc_map = await manager.get_collection_ids_for_agents(agent_ids)
        result = []
        for a in agents:
            resp = AgentResponse.model_validate(a)
            resp.kb_ids = kb_map.get(a.id, [])
            resp.collection_ids = wc_map.get(a.id, [])
            result.append(resp.model_dump(mode="json"))
        return ok(result)
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.post("/workspaces/{workspace_id}/agents", response_model=None)
async def create_agent(
    workspace_id: UUID,
    body: AgentCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = AgentManager(db)
        agent = await manager.create_agent(
            workspace_id=workspace_id,
            user_id=current_user.id,
            name=body.name,
            system_prompt=body.system_prompt,
            model_id=body.model_id,
            api_key_id=body.api_key_id,
            display_order=body.display_order,
            tools_config=[t.model_dump() for t in body.tools_config],
            kb_ids=body.kb_ids or [],
            collection_ids=body.collection_ids or [],
        )
        resp = AgentResponse.model_validate(agent)
        resp.kb_ids = body.kb_ids or []
        resp.collection_ids = body.collection_ids or []
        return ok(resp.model_dump(mode="json"), status_code=201)
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.post("/workspaces/{workspace_id}/agents/prompt-generate", response_model=None)
async def generate_prompt(
    workspace_id: UUID,
    body: PromptGenerateRequest,
    api_key_id: UUID = Query(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = AgentManager(db)
        result = await manager.generate_prompt(workspace_id, current_user.id, body.user_description, api_key_id)
        return ok(result.model_dump())
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.put("/agents/{agent_id}", response_model=None)
async def update_agent(
    agent_id: UUID,
    body: AgentUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = AgentManager(db)
        updates = {k: v for k, v in body.model_dump(exclude_none=True).items()}
        if "tools_config" in updates:
            updates["tools_config"] = [t if isinstance(t, dict) else t for t in updates["tools_config"]]
        agent = await manager.update_agent(agent_id, current_user.id, **updates)
        resp = AgentResponse.model_validate(agent)
        kb_map = await manager.get_kb_ids_for_agents([agent.id])
        wc_map = await manager.get_collection_ids_for_agents([agent.id])
        resp.kb_ids = kb_map.get(agent.id, [])
        resp.collection_ids = wc_map.get(agent.id, [])
        return ok(resp.model_dump(mode="json"))
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.delete("/agents/{agent_id}", response_model=None)
async def delete_agent(
    agent_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = AgentManager(db)
        await manager.delete_agent(agent_id, current_user.id)
        return ok(message="Agent deleted")
    except AppError as e:
        return fail(e.code, e.message, e.status_code)
