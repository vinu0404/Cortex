"""Embed endpoints — no JWT auth, embed_token IS the credential."""
import logging
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from app.embed.manager import EmbedManager
from config.settings import get_settings

router = APIRouter()
public_router = APIRouter()
logger = logging.getLogger(__name__)
settings = get_settings()


class EmbedStreamRequest(BaseModel):
    query: str
    conversation_id: UUID | None = None


@public_router.get("/embed/{token}")
async def serve_embed(token: str) -> Response:
    return await EmbedManager().serve_embed(token)


@router.post("/embed/{token}/stream")
async def embed_stream(token: str, body: EmbedStreamRequest, request: Request) -> StreamingResponse:
    return await EmbedManager().stream_embed(token, body, request)
