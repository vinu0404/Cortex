import asyncio
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from uuid import UUID

from celery_app import celery_app
from app.common.retry import async_redis_call
from config.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Celery task
# ---------------------------------------------------------------------------

class _WcTaskBase(celery_app.Task):
    abstract = True

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        url_id = args[0] if args else kwargs.get("url_id")
        if isinstance(exc, ValueError) and "not found" in str(exc).lower():
            logger.info("crawl_website_task: record deleted before task ran, url_id=%s", url_id)
            return
        logger.error("crawl_website_task permanently failed: url_id=%s err=%s", url_id, exc)
        if url_id:
            asyncio.run(_set_url_failed(url_id, str(exc)))


@celery_app.task(
    bind=True,
    base=_WcTaskBase,
    max_retries=2,
    acks_late=True,
    soft_time_limit=settings.WC_CRAWL_TIMEOUT_SECONDS + 30,
    time_limit=settings.WC_CRAWL_TIMEOUT_SECONDS + 60,
    name="web_pipeline.tasks.crawl_website_task",
)
def crawl_website_task(self, url_id: str) -> None:
    try:
        asyncio.run(_run_crawl_pipeline(url_id))
    except ValueError as exc:
        # ValueError = non-retriable (missing record, login_required, etc.)
        logger.error("crawl_website_task failed with non-retriable error: url_id=%s err=%s", url_id, exc, exc_info=True)
        raise
    except Exception as exc:
        logger.error("crawl_website_task retry: url_id=%s err=%s", url_id, exc)
        raise self.retry(exc=exc, countdown=10)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

async def _run_crawl_pipeline(url_id: str) -> None:
    import redis.asyncio as aioredis
    from database.session import get_custom_db_context_session
    from app.website_collections.db_models import WebsiteUrl, WcCrawlStatusEnum
    from app.api_keys.db_models import UserApiKey
    from app.connectors.encryption import decrypt_str
    from document_pipeline.embedder import embed_texts
    from document_pipeline.chunker import chunk_document
    from document_pipeline.parsers import ParsedChunkRaw
    from web_pipeline import vector_store as wc_vs
    from sqlalchemy import select

    redis_client = aioredis.from_url(settings.REDIS_URL)
    try:
        # 1. Load WebsiteUrl
        async with get_custom_db_context_session() as db:
            wu = await db.scalar(select(WebsiteUrl).where(WebsiteUrl.id == UUID(url_id)))
            if wu is None:
                raise ValueError(f"WebsiteUrl {url_id} not found — not retrying")
            if wu.crawl_status == WcCrawlStatusEnum.cancelled:
                logger.info("crawl_website_task skipped — url %s was cancelled", url_id)
                return

            collection_id = str(wu.collection_id)
            user_id = str(wu.user_id)
            url = wu.url
            max_depth = wu.max_depth

            # 2. Fetch API key
            key_result = await db.scalar(
                select(UserApiKey).where(UserApiKey.user_id == wu.user_id).limit(1)
            )
            if not key_result:
                raise ValueError(f"No API key for user {user_id} — not retrying")
            api_key = decrypt_str(key_result.encrypted_key)

            # 3. status = crawling
            wu.crawl_status = WcCrawlStatusEnum.crawling
            wu.error_message = None
            await db.commit()

        await _publish_wc_status(redis_client, user_id, collection_id, url_id, "crawling", url)

        # 4. Run spider in subprocess (Scrapy Twisted reactor can only start once per process)
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as tmp:
            output_path = tmp.name

        cfg = {
            "obey_robots": settings.WC_OBEY_ROBOTS,
            "user_agent": settings.WC_USER_AGENT,
            "concurrent_requests": settings.WC_CONCURRENT_REQUESTS,
            "download_timeout": settings.WC_DOWNLOAD_TIMEOUT,
        }

        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "web_pipeline.runner",
            url, str(max_depth), output_path, str(settings.WC_MAX_PAGES_PER_URL), json.dumps(cfg),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=settings.WC_CRAWL_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"Spider timed out after {settings.WC_CRAWL_TIMEOUT_SECONDS}s")

        stderr_output = (stderr_bytes or b"").decode(errors="replace").strip()

        if proc.returncode != 0:
            detail = f": {stderr_output[:500]}" if stderr_output else ""
            raise RuntimeError(f"Spider exited with code {proc.returncode}{detail}")

        # 5. Read JSONL output
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                items = [json.loads(line) for line in f if line.strip()]
        finally:
            try:
                os.unlink(output_path)
            except Exception as exc:
                logger.warning("Failed to remove crawler output file %s: %s", output_path, exc)

        pages = [item for item in items if not item.get("login_blocked")]
        blocked = [item for item in items if item.get("login_blocked")]
        login_blocked_count = len(blocked)
        start_url_blocked = any(b.get("is_start_url") for b in blocked)

        if start_url_blocked:
            raise ValueError(
                "login_required: Website requires login to access. "
                "Remove this URL and use a publicly accessible URL instead."
            )

        if not pages and login_blocked_count > 0:
            raise ValueError(
                "login_required: All pages on this website require login. "
                "Remove this URL and use a publicly accessible URL instead."
            )

        if not pages:
            detail = f" — spider log: {stderr_output[:300]}" if stderr_output else ""
            raise RuntimeError(f"Spider produced no pages — site may be empty, blocked, or unreachable{detail}")

        # 6. status = processing
        async with get_custom_db_context_session() as db:
            wu = await db.scalar(select(WebsiteUrl).where(WebsiteUrl.id == UUID(url_id)))
            if wu:
                wu.crawl_status = WcCrawlStatusEnum.processing
                wu.page_count = len(pages)
                wu.login_blocked_count = login_blocked_count
                await db.commit()

        await _publish_wc_status(
            redis_client, user_id, collection_id, url_id, "processing", url,
            page_count=len(pages), login_blocked_count=login_blocked_count,
        )

        # 7. Delete old chunks (idempotent re-crawl)
        await wc_vs.delete_url_chunks(collection_id, url_id)

        # 8. Chunk + embed
        all_chunks = []
        all_payloads = []
        for page in pages:
            raw = ParsedChunkRaw(
                text=page["text"],
                page_start=1,
                page_end=1,
                section=page.get("title"),
                chunk_type="text",
            )
            chunks = chunk_document([raw])
            for chunk in chunks:
                all_chunks.append(chunk)
                all_payloads.append({
                    "url": page["url"],
                    "title": page.get("title", ""),
                    "depth": page.get("depth", 0),
                })

        if not all_chunks:
            raise RuntimeError("Chunker produced no chunks from crawled pages")

        embeddings = await embed_texts([c.text for c in all_chunks], api_key)

        # 9. Upsert to Qdrant
        await wc_vs.ensure_collection(collection_id, redis_client)
        await wc_vs.upsert_chunks(collection_id, url_id, all_chunks, embeddings, all_payloads)

        # 10. status = ready
        async with get_custom_db_context_session() as db:
            wu = await db.scalar(select(WebsiteUrl).where(WebsiteUrl.id == UUID(url_id)))
            if wu:
                wu.crawl_status = WcCrawlStatusEnum.ready
                wu.page_count = len(pages)
                wu.chunk_count = len(all_chunks)
                wu.login_blocked_count = login_blocked_count
                wu.last_crawled_at = datetime.now(timezone.utc)
                wu.error_message = None
                await db.commit()

        await _publish_wc_status(
            redis_client, user_id, collection_id, url_id, "ready", url,
            page_count=len(pages), chunk_count=len(all_chunks), login_blocked_count=login_blocked_count,
        )
    finally:
        await redis_client.aclose()


async def _set_url_failed(url_id: str, error_message: str) -> None:
    import redis.asyncio as aioredis
    from database.session import get_custom_db_context_session
    from app.website_collections.db_models import WebsiteUrl, WcCrawlStatusEnum
    from sqlalchemy import select

    redis_client = aioredis.from_url(settings.REDIS_URL)
    try:
        async with get_custom_db_context_session() as db:
            wu = await db.scalar(select(WebsiteUrl).where(WebsiteUrl.id == UUID(url_id)))
            if wu:
                # Don't overwrite cancelled status with failed
                if wu.crawl_status == WcCrawlStatusEnum.cancelled:
                    return
                wu.crawl_status = WcCrawlStatusEnum.failed
                wu.error_message = error_message[:2000]
                await db.commit()
                await _publish_wc_status(
                    redis_client,
                    str(wu.user_id),
                    str(wu.collection_id),
                    url_id,
                    "failed",
                    wu.url,
                    error_message=error_message[:200],
                )
    except Exception:
        logger.exception("Failed to update WebsiteUrl status to failed: url_id=%s", url_id)
    finally:
        await redis_client.aclose()


async def _publish_wc_status(
    redis_client,
    user_id: str,
    collection_id: str,
    url_id: str,
    status: str,
    url: str,
    page_count: int | None = None,
    chunk_count: int | None = None,
    login_blocked_count: int | None = None,
    error_message: str | None = None,
) -> None:
    payload: dict = {
        "collection_id": collection_id,
        "url_id": url_id,
        "status": status,
        "url": url,
    }
    if page_count is not None:
        payload["page_count"] = page_count
    if chunk_count is not None:
        payload["chunk_count"] = chunk_count
    if login_blocked_count is not None:
        payload["login_blocked_count"] = login_blocked_count
    if error_message:
        payload["error_message"] = error_message
    try:
        await async_redis_call(redis_client, "publish", f"wc_status:{user_id}", json.dumps(payload))
    except Exception:
        logger.error("Failed to publish WC status: %s", payload)
