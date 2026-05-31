from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class VinuSettingsResponse(BaseModel):
    vinu_agent_name: str | None
    model_config = {"from_attributes": True}


class VinuSettingsUpdate(BaseModel):
    vinu_agent_name: str | None = None



class VinuConversationResponse(BaseModel):
    id: UUID
    name: str
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class VinuChatRequest(BaseModel):
    conversation_id: UUID | None = None
    message: str


class VinuBuildRequest(BaseModel):
    plan: dict
    conversation_id: UUID | None = None


@dataclass
class VinuChatResult:
    conv_id: UUID
    is_new: bool
    reply: str
    phase: str
    plan: dict | None
    questions: list | None
    new_title: str | None
    was_compressed: bool = False
