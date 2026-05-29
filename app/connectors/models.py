from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.connectors.db_models import AuthTypeEnum, ConnectorStatusEnum


class ConnectorDefinitionResponse(BaseModel):
    id: UUID
    slug: str
    display_name: str
    auth_type: AuthTypeEnum
    tools: list[dict]
    icon: str | None

    model_config = {"from_attributes": True}


class ConnectorInstanceResponse(BaseModel):
    id: UUID
    user_id: UUID
    definition_id: UUID
    account_label: str | None
    status: ConnectorStatusEnum
    slug: str
    display_name: str
    created_at: datetime

    model_config = {"from_attributes": True}


class AuthUrlResponse(BaseModel):
    auth_url: str
    state: str
