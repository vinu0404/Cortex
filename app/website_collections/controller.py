import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.db_models import User
from app.common.api_response import fail, ok
from app.common.exceptions import AppError
from app.common.redis_client import get_async_redis
from app.website_collections.manager import WebsiteCollectionManager
from app.website_collections.models import (
    AddUrlRequest,
    WebsiteCollectionCreate,
    WebsiteCollectionResponse,
    WebsiteUrlResponse,
)
from config.settings import get_settings
from database.session import get_db, get_custom_db_context_session
from web_pipeline.vector_store import list_url_chunks

router = APIRouter()
logger = logging.getLogger(__name__)
settings = get_settings()


# --- SSE must be registered BEFORE /{collection_id} routes ---

@router.get("/website-collections/status/stream")
async def wc_status_stream(token: str) -> StreamingResponse:
    from jose import JWTError, jwt
    from app.auth.manager import AuthManager
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
        if payload.get("type") != "access":
            raise ValueError
        user_id_str = payload.get("sub")
        if not user_id_str:
            raise ValueError
        async with get_custom_db_context_session() as db:
            user = await AuthManager(db).get_user_by_id(UUID(user_id_str))
            if not user or not user.is_active:
                raise ValueError
    except (JWTError, ValueError) as e:
        logger.error("WC SSE auth rejected: %s — %s", type(e).__name__, e)
        return Response("Unauthorized", status_code=401)

    user_id = user.id

    async def event_generator():
        redis = get_async_redis()
        channel = f"wc_status:{user_id}"
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel)
        try:
            yield "data: connected\n\n"
            while True:
                try:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=4.0
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    yield ": keepalive\n\n"
                    await asyncio.sleep(1)
                    try:
                        await pubsub.subscribe(channel)
                    except Exception:
                        pass
                    continue
                if message and message["type"] == "message":
                    yield f"data: {message['data']}\n\n"
                else:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# --- Collections ---

@router.post("/website-collections", response_model=None)
async def create_collection(
    body: WebsiteCollectionCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        mgr = WebsiteCollectionManager(db)
        wc = await mgr.create_collection(current_user.id, body.name, body.description)
        return ok(WebsiteCollectionResponse.model_validate(wc).model_dump(mode="json"), status_code=201)
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.get("/website-collections", response_model=None)
async def list_collections(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        mgr = WebsiteCollectionManager(db)
        wcs = await mgr.list_collections(current_user.id)
        return ok([WebsiteCollectionResponse.model_validate(wc).model_dump(mode="json") for wc in wcs])
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.delete("/website-collections/{collection_id}", response_model=None)
async def delete_collection(
    collection_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        mgr = WebsiteCollectionManager(db)
        await mgr.delete_collection(collection_id, current_user.id)
        return ok({"deleted": True})
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


# --- URLs ---

@router.post("/website-collections/{collection_id}/urls", response_model=None)
async def add_url(
    collection_id: UUID,
    body: AddUrlRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        mgr = WebsiteCollectionManager(db)
        wu = await mgr.add_url(collection_id, current_user.id, body.url, body.max_depth)
        return ok(WebsiteUrlResponse.model_validate(wu).model_dump(mode="json"), status_code=201)
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.get("/website-collections/{collection_id}/urls", response_model=None)
async def list_urls(
    collection_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        mgr = WebsiteCollectionManager(db)
        urls = await mgr.list_urls(collection_id, current_user.id)
        return ok([WebsiteUrlResponse.model_validate(u).model_dump(mode="json") for u in urls])
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.get("/website-collections/{collection_id}/urls/{url_id}/chunks", response_model=None)
async def get_url_chunks(
    collection_id: UUID,
    url_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        mgr = WebsiteCollectionManager(db)
        await mgr.get_url(collection_id, url_id, current_user.id)
        chunks = await list_url_chunks(str(collection_id), str(url_id))
        return ok({"chunks": chunks, "count": len(chunks)})
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.delete("/website-collections/{collection_id}/urls/{url_id}", response_model=None)
async def delete_url(
    collection_id: UUID,
    url_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        mgr = WebsiteCollectionManager(db)
        await mgr.delete_url(collection_id, url_id, current_user.id)
        return ok({"deleted": True})
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.post("/website-collections/{collection_id}/urls/{url_id}/scrape", response_model=None)
async def scrape_url(
    collection_id: UUID,
    url_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        mgr = WebsiteCollectionManager(db)
        wu = await mgr.trigger_scrape(collection_id, url_id, current_user.id)
        return ok(WebsiteUrlResponse.model_validate(wu).model_dump(mode="json"))
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.post("/website-collections/{collection_id}/scrape", response_model=None)
async def scrape_all(
    collection_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        mgr = WebsiteCollectionManager(db)
        urls = await mgr.trigger_scrape_all(collection_id, current_user.id)
        return ok([WebsiteUrlResponse.model_validate(u).model_dump(mode="json") for u in urls])
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.post("/website-collections/{collection_id}/urls/{url_id}/retry", response_model=None)
async def retry_url(
    collection_id: UUID,
    url_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        mgr = WebsiteCollectionManager(db)
        wu = await mgr.retry_url(collection_id, url_id, current_user.id)
        return ok(WebsiteUrlResponse.model_validate(wu).model_dump(mode="json"))
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.post("/website-collections/{collection_id}/urls/{url_id}/cancel", response_model=None)
async def cancel_url_processing(
    collection_id: UUID,
    url_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        mgr = WebsiteCollectionManager(db)
        await mgr.cancel_url(collection_id, url_id, current_user.id)
        return ok({"cancelled": True})
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.post("/website-collections/{collection_id}/reindex", response_model=None)
async def reindex_collection(
    collection_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        mgr = WebsiteCollectionManager(db)
        count = await mgr.reindex_collection(collection_id, current_user.id)
        return ok({"queued": count})
    except AppError as e:
        return fail(e.code, e.message, e.status_code)
