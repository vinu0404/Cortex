from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr

from app.auth.db_models import RoleEnum


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: UUID
    email: str
    role: RoleEnum
    is_active: bool
    created_at: datetime
    timezone: str = "UTC"
    vinu_agent_name: str | None = None

    model_config = {"from_attributes": True}


class UserUpdate(BaseModel):
    timezone: str | None = None
    vinu_agent_name: str | None = None
