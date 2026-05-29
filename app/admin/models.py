from pydantic import BaseModel

from app.auth.db_models import RoleEnum


class AdminUserUpdateRequest(BaseModel):
    is_active: bool | None = None
    role: RoleEnum | None = None
