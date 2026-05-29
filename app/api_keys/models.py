from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class ApiKeyCreate(BaseModel):
    key_name: str
    api_key: str


class ApiKeyResponse(BaseModel):
    id: UUID
    user_id: UUID
    key_name: str
    provider: str | None
    available_models: list[str]
    masked_key: str
    created_at: datetime

    model_config = {"from_attributes": True}
