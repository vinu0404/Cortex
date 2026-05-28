from datetime import datetime
from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel

T = TypeVar("T")


class CursorPage(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None
    has_next: bool


def encode_cursor(created_at: datetime, id: UUID) -> str:
    import base64
    raw = f"{created_at.isoformat()}:{id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    import base64
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    ts_str, id_str = raw.split(":", 1)
    return datetime.fromisoformat(ts_str), UUID(id_str)


def build_cursor_page(items: list[Any], limit: int) -> CursorPage:
    has_next = len(items) > limit
    page_items = items[:limit]
    next_cursor = None
    if has_next and page_items:
        last = page_items[-1]
        next_cursor = encode_cursor(last.created_at, last.id)
    return CursorPage(items=page_items, next_cursor=next_cursor, has_next=has_next)
