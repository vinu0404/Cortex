from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class ParseScheduleRequest(BaseModel):
    natural_query: str
    timezone: str = "UTC"


class ParseScheduleResponse(BaseModel):
    cron_expr: str
    human_schedule: str
    task_description: str
    agents: list[dict]
    tools_needed: list[str]


class CronJobCreate(BaseModel):
    name: str
    natural_query: str
    cron_expr: str
    human_schedule: str
    timezone: str = "UTC"
    task_description: str = ""
    agents: list[dict] = []
    tools_needed: list[str] = []


class CronJobUpdate(BaseModel):
    natural_query: str
    cron_expr: str
    human_schedule: str


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
    last_run_at: datetime | None
    next_run_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
