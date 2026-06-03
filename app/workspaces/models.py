from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints

NonEmptyTrimmedString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
OptionalNonEmptyTrimmedString = Annotated[str | None, StringConstraints(strip_whitespace=True, min_length=1)]


class WorkspaceCreate(BaseModel):
    name: NonEmptyTrimmedString
    description: NonEmptyTrimmedString
    workspace_type: str = "standard"


class WorkspaceUpdate(BaseModel):
    name: OptionalNonEmptyTrimmedString = None
    description: OptionalNonEmptyTrimmedString = None


class WorkspaceResponse(BaseModel):
    id: UUID
    user_id: UUID
    name: str
    description: str | None
    workspace_type: str = "standard"
    embed_enabled: bool
    embed_token: str | None = None
    embed_hitl_auto_approve: bool
    embed_budget_usd: float | None = None
    embed_budget_tokens: int | None = None
    embed_spend_usd: float = 0.0
    embed_spend_tokens: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class WorkspaceEmbedResponse(BaseModel):
    embed_enabled: bool
    embed_token: str | None
    embed_hitl_auto_approve: bool
    embed_budget_usd: float | None = None
    embed_budget_tokens: int | None = None
    embed_spend_usd: float = 0.0
    embed_spend_tokens: int = 0
    embed_url: str | None
    snippet: str | None


class WorkspaceEmbedUpdate(BaseModel):
    # None = field not sent (no change); 0 = clear limit; positive = set limit
    embed_hitl_auto_approve: bool | None = None
    embed_budget_usd: float | None = Field(default=None, ge=0)
    embed_budget_tokens: int | None = Field(default=None, ge=0)
