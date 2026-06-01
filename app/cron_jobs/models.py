from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class AgentPlanItem(BaseModel):
    name: str
    role: str
    tools: list[str] = []
    kb_names: list[str] = []
    wc_names: list[str] = []


class ParseScheduleRequest(BaseModel):
    natural_query: str
    timezone: str = "UTC"


class RefinePlanRequest(BaseModel):
    natural_query: str
    current_agents: list[dict]
    change_request: str
    timezone: str = "UTC"


class CronJobCreate(BaseModel):
    name: str
    natural_query: str
    cron_expr: str
    human_schedule: str
    timezone: str = "UTC"
    task_description: str = ""
    agents: list[AgentPlanItem] = []
    tools_needed: list[str] = []


class CronJobUpdate(BaseModel):
    natural_query: str
    cron_expr: str
    human_schedule: str
    timezone: str | None = None


class ToggleJobRequest(BaseModel):
    is_active: bool


class CronJobResponse(BaseModel):
    id: UUID
    user_id: UUID
    workspace_id: UUID
    name: str
    natural_query: str
    cron_expr: str
    human_schedule: str
    timezone: str
    is_active: bool
    task_description: str
    agent_plan: list[dict]
    last_run_at: datetime | None
    next_run_at: datetime | None
    celery_task_id: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
