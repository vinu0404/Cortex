import logging
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.website_collections.db_models import (
    AgentWebsiteCollection,
    WebsiteCollection,
    WebsiteUrl,
    WcCrawlStatusEnum,
)
from config.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = (WcCrawlStatusEnum.crawling, WcCrawlStatusEnum.processing)

_CANCELLABLE_STATUSES = {
    WcCrawlStatusEnum.pending,
    WcCrawlStatusEnum.crawling,
    WcCrawlStatusEnum.processing,
}

_REINDEXABLE_STATUSES = {
    WcCrawlStatusEnum.ready,
    WcCrawlStatusEnum.failed,
    WcCrawlStatusEnum.cancelled,
}


class WebsiteCollectionManager:
    def __init__(self, db: AsyncSession):
        self._db = db

    # ------------------------------------------------------------------
    # Collections
    # ------------------------------------------------------------------

    async def list_collections(self, user_id: UUID) -> list[WebsiteCollection]:
        result = await self._db.scalars(
            select(WebsiteCollection)
            .where(WebsiteCollection.user_id == user_id)
            .order_by(WebsiteCollection.created_at.desc())
        )
        return list(result)

    async def create_collection(self, user_id: UUID, name: str, description: str | None) -> WebsiteCollection:
        wc = WebsiteCollection(user_id=user_id, name=name, description=description)
        self._db.add(wc)
        await self._db.flush()
        return wc

    async def delete_collection(self, collection_id: UUID, user_id: UUID) -> None:
        wc = await self._get_collection(collection_id, user_id)
        await self._db.delete(wc)
        await self._db.commit()

        from web_pipeline import vector_store as wc_vs
        await wc_vs.delete_collection(str(collection_id))

    # ------------------------------------------------------------------
    # URLs
    # ------------------------------------------------------------------

    async def list_urls(self, collection_id: UUID, user_id: UUID) -> list[WebsiteUrl]:
        await self._get_collection(collection_id, user_id)
        result = await self._db.scalars(
            select(WebsiteUrl)
            .where(WebsiteUrl.collection_id == collection_id)
            .order_by(WebsiteUrl.created_at.asc())
        )
        return list(result)

    async def add_url(self, collection_id: UUID, user_id: UUID, url: str, max_depth: int) -> WebsiteUrl:
        wc = await self._get_collection(collection_id, user_id)
        if wc.url_count >= settings.WC_MAX_URLS_PER_COLLECTION:
            raise ConflictError(f"Max {settings.WC_MAX_URLS_PER_COLLECTION} URLs per collection")

        wu = WebsiteUrl(
            collection_id=collection_id,
            user_id=user_id,
            url=url,
            max_depth=max(1, min(max_depth, settings.WC_MAX_DEPTH)),
        )
        self._db.add(wu)
        await self._db.flush()

        await self._db.execute(
            update(WebsiteCollection)
            .where(WebsiteCollection.id == collection_id)
            .values(url_count=WebsiteCollection.url_count + 1)
        )
        await self._db.commit()
        return wu

    async def delete_url(self, collection_id: UUID, url_id: UUID, user_id: UUID) -> None:
        wu = await self.get_url(collection_id, url_id, user_id)

        task_id = wu.celery_task_id
        await self._db.delete(wu)
        await self._db.execute(
            update(WebsiteCollection)
            .where(WebsiteCollection.id == collection_id)
            .values(url_count=WebsiteCollection.url_count - 1)
        )
        await self._db.commit()

        if task_id:
            try:
                from celery_app import celery_app
                celery_app.control.revoke(task_id, terminate=True)
            except Exception:
                logger.warning("Could not revoke task %s for url %s — task may still run", task_id, url_id)

        from web_pipeline import vector_store as wc_vs
        await wc_vs.delete_url_chunks(str(collection_id), str(url_id))

    async def trigger_scrape(self, collection_id: UUID, url_id: UUID, user_id: UUID) -> WebsiteUrl:
        wu = await self.get_url(collection_id, url_id, user_id)
        if wu.crawl_status in _ACTIVE_STATUSES:
            raise ConflictError("URL is already being crawled")

        wu.crawl_status = WcCrawlStatusEnum.pending
        wu.error_message = None
        await self._db.commit()

        from web_pipeline.tasks import crawl_website_task
        result = crawl_website_task.delay(str(url_id))
        wu.celery_task_id = result.id
        await self._db.commit()
        return wu

    async def trigger_scrape_all(self, collection_id: UUID, user_id: UUID) -> list[WebsiteUrl]:
        urls = await self.list_urls(collection_id, user_id)
        triggered = []
        for wu in urls:
            if wu.crawl_status not in _ACTIVE_STATUSES:
                wu.crawl_status = WcCrawlStatusEnum.pending
                wu.error_message = None
                triggered.append(wu)
        if triggered:
            await self._db.commit()
            from web_pipeline.tasks import crawl_website_task
            for wu in triggered:
                result = crawl_website_task.delay(str(wu.id))
                wu.celery_task_id = result.id
            await self._db.commit()
        return urls

    async def retry_url(self, collection_id: UUID, url_id: UUID, user_id: UUID) -> WebsiteUrl:
        wu = await self.get_url(collection_id, url_id, user_id)
        if wu.crawl_status != WcCrawlStatusEnum.failed:
            raise ConflictError("Only failed URLs can be retried")
        # login_required errors should not be retried — UI enforces Remove, but guard here too
        if wu.error_message and wu.error_message.startswith("login_required:"):
            raise ConflictError("Login-blocked URLs cannot be retried — remove and add a public URL instead")

        wu.crawl_status = WcCrawlStatusEnum.pending
        wu.error_message = None
        await self._db.commit()

        from web_pipeline.tasks import crawl_website_task
        result = crawl_website_task.delay(str(url_id))
        wu.celery_task_id = result.id
        await self._db.commit()
        return wu

    async def cancel_url(self, collection_id: UUID, url_id: UUID, user_id: UUID) -> WebsiteUrl:
        wu = await self.get_url(collection_id, url_id, user_id)
        if wu.crawl_status not in _CANCELLABLE_STATUSES:
            raise ConflictError("URL cannot be cancelled in its current state")

        task_id = wu.celery_task_id
        wu.crawl_status = WcCrawlStatusEnum.cancelled
        wu.celery_task_id = None
        await self._db.commit()

        if task_id:
            from celery_app import celery_app
            celery_app.control.revoke(task_id, terminate=True)

        return wu

    async def reindex_collection(self, collection_id: UUID, user_id: UUID) -> int:
        """Re-scrape all ready/failed/cancelled URLs. Crawl task deletes old chunks automatically."""
        urls = await self.list_urls(collection_id, user_id)

        to_dispatch: list[WebsiteUrl] = []
        for wu in urls:
            if wu.crawl_status not in _REINDEXABLE_STATUSES:
                continue
            wu.crawl_status = WcCrawlStatusEnum.pending
            wu.error_message = None
            wu.page_count = 0
            wu.chunk_count = 0
            to_dispatch.append(wu)

        if not to_dispatch:
            return 0

        await self._db.commit()

        from web_pipeline.tasks import crawl_website_task
        for wu in to_dispatch:
            result = crawl_website_task.delay(str(wu.id))
            wu.celery_task_id = result.id
        await self._db.commit()

        return len(to_dispatch)

    # ------------------------------------------------------------------
    # Agent ↔ collection junction
    # ------------------------------------------------------------------

    async def set_agent_website_collections(self, agent_id: UUID, collection_ids: list[UUID]) -> None:
        existing = list(await self._db.scalars(
            select(AgentWebsiteCollection).where(AgentWebsiteCollection.agent_id == agent_id)
        ))
        for row in existing:
            await self._db.delete(row)
        for cid in collection_ids:
            self._db.add(AgentWebsiteCollection(agent_id=agent_id, collection_id=cid))
        await self._db.flush()

    async def get_collection_ids_for_agent(self, agent_id: UUID) -> list[UUID]:
        result = await self._db.scalars(
            select(AgentWebsiteCollection).where(AgentWebsiteCollection.agent_id == agent_id)
        )
        return [row.collection_id for row in result]

    async def get_collection_ids_for_agents(self, agent_ids: list[UUID]) -> dict[UUID, list[UUID]]:
        if not agent_ids:
            return {}
        rows = list(await self._db.scalars(
            select(AgentWebsiteCollection).where(AgentWebsiteCollection.agent_id.in_(agent_ids))
        ))
        result: dict[UUID, list[UUID]] = {aid: [] for aid in agent_ids}
        for row in rows:
            result[row.agent_id].append(row.collection_id)
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_collection(self, collection_id: UUID, user_id: UUID) -> WebsiteCollection:
        wc = await self._db.get(WebsiteCollection, collection_id)
        if not wc:
            raise NotFoundError("WebsiteCollection", str(collection_id))
        if wc.user_id != user_id:
            raise ForbiddenError("Access denied")
        return wc

    async def get_url(self, collection_id: UUID, url_id: UUID, user_id: UUID) -> WebsiteUrl:
        wu = await self._db.get(WebsiteUrl, url_id)
        if not wu or wu.collection_id != collection_id:
            raise NotFoundError("WebsiteUrl", str(url_id))
        if wu.user_id != user_id:
            raise ForbiddenError("Access denied")
        return wu
