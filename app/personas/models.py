from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class PersonaCreate(BaseModel):
    name: str
    description: str | None = None
    system_prompt: str


class PersonaUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    system_prompt: str | None = None


class PersonaResponse(BaseModel):
    id: UUID
    user_id: UUID
    name: str
    description: str | None
    system_prompt: str
    created_at: datetime

    model_config = {"from_attributes": True}
