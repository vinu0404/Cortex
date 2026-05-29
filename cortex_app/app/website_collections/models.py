from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class WebsiteCollectionCreate(BaseModel):
    name: str
    description: str | None = None


class WebsiteCollectionResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    url_count: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AddUrlRequest(BaseModel):
    url: str
    max_depth: int = 2


class WebsiteUrlResponse(BaseModel):
    id: UUID
    collection_id: UUID
    url: str
    max_depth: int
    crawl_status: str
    page_count: int
    chunk_count: int
    login_blocked_count: int
    error_message: str | None
    last_crawled_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}
