from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class KnowledgeBaseCreate(BaseModel):
    name: str
    description: str | None = None


class KnowledgeBaseResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    document_count: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class KbDocumentResponse(BaseModel):
    id: UUID
    kb_id: UUID
    filename: str
    file_size_bytes: int | None
    source_type: str
    processing_status: str
    chunk_count: int
    embedding_model: str | None
    indexed_at: datetime | None
    error_message: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class PresignUploadRequest(BaseModel):
    filename: str
    content_type: str
    file_size_bytes: int


class S3IngestRequest(BaseModel):
    url: str
    access_key_id: str | None = None
    secret_access_key: str | None = None
    region: str | None = None
    filename: str


class RetryResponse(BaseModel):
    doc_id: UUID
    status: str
