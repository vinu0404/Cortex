from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.db_models import AdminModelService
from app.admin.models import AdminUserUpdateRequest
from app.auth.db_models import User
from app.common.exceptions import NotFoundError
from app.common.pagination import decode_cursor


class AdminManager:
    def __init__(self, db: AsyncSession):
        self._admin_model_service = AdminModelService(db)

    async def get_stats(self) -> dict:
        return await self._admin_model_service.get_stats()

    async def update_user(self, user_id: UUID, body: AdminUserUpdateRequest) -> dict:
        user = await self._admin_model_service.get_user(user_id)
        if not user:
            raise NotFoundError("User", str(user_id))
        user = await self._admin_model_service.update_user(user, body)
        return _user_dict(user)

    async def list_table(self, table: str, cursor: str | None, limit: int) -> dict:
        cursor_created_at, cursor_id = decode_cursor(cursor) if cursor else (None, None)
        return await self._admin_model_service.list_table(table, cursor_created_at, cursor_id, limit)

    async def list_junction(self, table: str, limit: int) -> dict:
        return await self._admin_model_service.list_junction(table, limit)


def _user_dict(user: User) -> dict:
    return {
        "id": str(user.id),
        "email": user.email,
        "role": user.role.value,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat(),
    }
