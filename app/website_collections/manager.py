import logging
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import ConflictError
from app.website_collections.db_models import (
    WebsiteCollection,
    WebsiteCollectionModelService,
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
        self._wc_model_service = WebsiteCollectionModelService(db)

    async def list_collections(self, user_id: UUID) -> list[WebsiteCollection]:
        return await self._wc_model_service.list_collections(user_id)

    async def create_collection(self, user_id: UUID, name: str, description: str | None) -> WebsiteCollection:
        try:
            return await self._wc_model_service.create_collection(user_id, name, description)
        except IntegrityError as e:
            await self._wc_model_service.undo_changes()
            raise ConflictError(f'A website collection named "{name}" already exists.') from e

    async def delete_collection(self, collection_id: UUID, user_id: UUID) -> None:
        wc = await self._wc_model_service.get_collection(collection_id, user_id)
        await self._wc_model_service.delete_collection(wc)
        await self._wc_model_service.save_changes()
        from web_pipeline import vector_store as wc_vs
        await wc_vs.delete_collection(str(collection_id))

    async def list_urls(self, collection_id: UUID, user_id: UUID) -> list[WebsiteUrl]:
        await self._wc_model_service.get_collection(collection_id, user_id)
        return await self._wc_model_service.list_urls(collection_id)

    async def add_url(self, collection_id: UUID, user_id: UUID, url: str, max_depth: int) -> WebsiteUrl:
        wc = await self._wc_model_service.get_collection(collection_id, user_id)
        if wc.url_count >= settings.WC_MAX_URLS_PER_COLLECTION:
            raise ConflictError(f"Max {settings.WC_MAX_URLS_PER_COLLECTION} URLs per collection")
        wu = await self._wc_model_service.create_url(
            collection_id,
            user_id,
            url,
            max(1, min(max_depth, settings.WC_MAX_DEPTH)),
        )
        await self._wc_model_service.save_changes()
        return wu

    async def delete_url(self, collection_id: UUID, url_id: UUID, user_id: UUID) -> None:
        wu = await self.get_url(collection_id, url_id, user_id)
        task_id = await self._wc_model_service.delete_url(collection_id, wu)
        await self._wc_model_service.save_changes()
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
        await self._wc_model_service.set_url_pending(wu)
        await self._wc_model_service.save_changes()
        from web_pipeline.tasks import crawl_website_task
        result = crawl_website_task.delay(str(url_id))
        await self._wc_model_service.set_url_task(wu, result.id)
        await self._wc_model_service.save_changes()
        return wu

    async def trigger_scrape_all(self, collection_id: UUID, user_id: UUID) -> list[WebsiteUrl]:
        urls = await self.list_urls(collection_id, user_id)
        triggered = [wu for wu in urls if wu.crawl_status not in _ACTIVE_STATUSES]
        if not triggered:
            return urls
        for wu in triggered:
            await self._wc_model_service.set_url_pending(wu)
        await self._wc_model_service.save_changes()
        from web_pipeline.tasks import crawl_website_task
        for wu in triggered:
            result = crawl_website_task.delay(str(wu.id))
            await self._wc_model_service.set_url_task(wu, result.id)
        await self._wc_model_service.save_changes()
        return urls

    async def retry_url(self, collection_id: UUID, url_id: UUID, user_id: UUID) -> WebsiteUrl:
        wu = await self.get_url(collection_id, url_id, user_id)
        if wu.crawl_status != WcCrawlStatusEnum.failed:
            raise ConflictError("Only failed URLs can be retried")
        if wu.error_message and wu.error_message.startswith("login_required:"):
            raise ConflictError("Login-blocked URLs cannot be retried — remove and add a public URL instead")
        await self._wc_model_service.set_url_pending(wu)
        await self._wc_model_service.save_changes()
        from web_pipeline.tasks import crawl_website_task
        result = crawl_website_task.delay(str(url_id))
        await self._wc_model_service.set_url_task(wu, result.id)
        await self._wc_model_service.save_changes()
        return wu

    async def cancel_url(self, collection_id: UUID, url_id: UUID, user_id: UUID) -> WebsiteUrl:
        wu = await self.get_url(collection_id, url_id, user_id)
        if wu.crawl_status not in _CANCELLABLE_STATUSES:
            raise ConflictError("URL cannot be cancelled in its current state")
        wu, task_id = await self._wc_model_service.cancel_url(wu)
        await self._wc_model_service.save_changes()
        if task_id:
            from celery_app import celery_app
            celery_app.control.revoke(task_id, terminate=True)
        return wu

    async def reindex_collection(self, collection_id: UUID, user_id: UUID) -> int:
        urls = await self.list_urls(collection_id, user_id)
        to_dispatch = []
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
        await self._wc_model_service.save_changes()
        from web_pipeline.tasks import crawl_website_task
        for wu in to_dispatch:
            result = crawl_website_task.delay(str(wu.id))
            await self._wc_model_service.set_url_task(wu, result.id)
        await self._wc_model_service.save_changes()
        return len(to_dispatch)

    async def set_agent_website_collections(self, agent_id: UUID, collection_ids: list[UUID]) -> None:
        await self._wc_model_service.set_agent_collections(agent_id, collection_ids)

    async def get_collection_ids_for_agent(self, agent_id: UUID) -> list[UUID]:
        return await self._wc_model_service.get_collection_ids_for_agent(agent_id)

    async def get_collection_ids_for_agents(self, agent_ids: list[UUID]) -> dict[UUID, list[UUID]]:
        if not agent_ids:
            return {}
        rows = await self._wc_model_service.get_agent_collection_links(agent_ids)
        result: dict[UUID, list[UUID]] = {agent_id: [] for agent_id in agent_ids}
        for row in rows:
            result[row.agent_id].append(row.collection_id)
        return result

    async def get_url(self, collection_id: UUID, url_id: UUID, user_id: UUID) -> WebsiteUrl:
        return await self._wc_model_service.get_url(collection_id, url_id, user_id)
