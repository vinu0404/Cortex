from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.agents.db_models import AgentTypeEnum


class ToolConfig(BaseModel):
    tool: str
    connector_slug: str
    requires_hitl: bool = False


class AgentCreate(BaseModel):
    name: str
    system_prompt: str | None = None
    model_id: str | None = None
    api_key_id: UUID | None = None
    display_order: int = 0
    tools_config: list[ToolConfig] = []


class AgentUpdate(BaseModel):
    name: str | None = None
    system_prompt: str | None = None
    model_id: str | None = None
    api_key_id: UUID | None = None
    display_order: int | None = None
    tools_config: list[ToolConfig] | None = None


class AgentResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    name: str
    system_prompt: str | None
    agent_type: AgentTypeEnum
    model_id: str | None
    api_key_id: UUID | None
    display_order: int
    is_editable: bool
    tools_config: list[dict]
    created_at: datetime

    model_config = {"from_attributes": True}


class PromptGenerateRequest(BaseModel):
    user_description: str


class PromptGenerateResponse(BaseModel):
    generated_prompt: str
    recommended_tools: list[dict]
    recommended_mcp: list = []
