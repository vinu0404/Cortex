from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.chat.db_models import HitlStatusEnum, MessageRoleEnum


class ConversationCreate(BaseModel):
    workspace_id: UUID


class ConversationResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    title: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class MessageResponse(BaseModel):
    id: UUID
    role: MessageRoleEnum
    content: str
    total_cost_usd: float | None
    latency_ms: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatStreamRequest(BaseModel):
    workspace_id: UUID
    conversation_id: UUID | None = None
    query: str
    persona_id: UUID | None = None


class HitlRespondRequest(BaseModel):
    request_id: UUID
    approved: bool
    instructions: str | None = None
