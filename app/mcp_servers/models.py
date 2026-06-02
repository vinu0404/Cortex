from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class MCPServerCreate(BaseModel):
    name: str
    transport_type: str = "http"
    server_url: str = ""
    auth_type: str = "none"
    auth_header_name: str | None = None
    token: str | None = None
    command: str | None = None
    env_vars: dict[str, str] | None = None


class MCPServerUpdate(BaseModel):
    name: str | None = None
    transport_type: str | None = None
    server_url: str | None = None
    auth_type: str | None = None
    auth_header_name: str | None = None
    token: str | None = None
    command: str | None = None
    env_vars: dict[str, str] | None = None


class MCPToolHITLUpdate(BaseModel):
    requires_hitl: bool


class MCPServerResponse(BaseModel):
    id: UUID
    name: str
    transport_type: str
    server_url: str
    auth_type: str
    auth_header_name: str | None
    command: str | None
    discovered_tools: list[dict]
    is_active: bool
    last_synced_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}
