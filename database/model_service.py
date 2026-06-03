from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


class AsyncModelService:
    """DB-layer transaction helpers for the current async SQLAlchemy stack."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def save_changes(self) -> None:
        await self._session.commit()

    async def undo_changes(self) -> None:
        await self._session.rollback()

    async def refresh(self, instance: Any) -> None:
        await self._session.refresh(instance)
