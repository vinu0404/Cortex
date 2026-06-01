import logging
from datetime import datetime, timezone
from uuid import UUID

from celery.schedules import crontab
from redbeat import RedBeatSchedulerEntry
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from croniter import croniter as _croniter

from app.cron_jobs.db_models import CronJob
from app.common.exceptions import AppError, ForbiddenError, NotFoundError
from app.workspaces.manager import WorkspaceManager
from celery_app import celery_app

logger = logging.getLogger(__name__)


class CronJobManager:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def list_jobs(self, user_id: UUID) -> list[CronJob]:
        result = await self._db.scalars(
            select(CronJob)
            .where(CronJob.user_id == user_id)
            .order_by(CronJob.created_at.desc())
        )
        return list(result)

    async def get_job(self, job_id: UUID, user_id: UUID) -> CronJob:
        job = await self._db.get(CronJob, job_id)
        if not job:
            raise NotFoundError("CronJob", str(job_id))
        if job.user_id != user_id:
            raise ForbiddenError("Access denied")
        return job

    async def create_job(
        self,
        user_id: UUID,
        name: str,
        natural_query: str,
        cron_expr: str,
        human_schedule: str,
        tz: str,
        task_description: str,
        agent_plan: list[dict],
    ) -> CronJob:
        api_key_id = await _get_user_api_key_id(user_id, self._db)
        if not api_key_id:
            raise AppError("NO_API_KEY", "Add an API key before creating a cron job", 400)

        if not _croniter.is_valid(cron_expr):
            raise AppError("INVALID_CRON", f"Invalid cron expression: {cron_expr}", 400)

        # Workspace created in same transaction — rolls back together on failure
        ws = await WorkspaceManager(self._db).create_workspace(
            user_id, name, task_description or natural_query, workspace_type="cron"
        )
        ws.embed_hitl_auto_approve = True

        # Create custom agents from AI plan so tools are available at run time
        await self._create_planned_agents(ws.id, user_id, agent_plan, api_key_id=api_key_id)

        job = CronJob(
            user_id=user_id,
            workspace_id=ws.id,
            name=name,
            natural_query=natural_query,
            cron_expr=cron_expr,
            human_schedule=human_schedule,
            timezone=tz,
            is_active=True,
            task_description=task_description,
            agent_plan=agent_plan,
        )
        job.next_run_at = _compute_next_run(cron_expr)
        self._db.add(job)
        await self._db.flush()

        # Commit before registering beat — prevents ghost schedules on rollback
        await self._db.commit()
        await self._db.refresh(job)
        self._register_beat(job)
        return job

    async def _create_planned_agents(
        self, workspace_id: UUID, user_id: UUID, agent_plan: list[dict], api_key_id: UUID | None = None
    ) -> None:
        if not agent_plan:
            return
        from app.agents.manager import AgentManager
        from app.knowledge_bases.manager import KnowledgeBaseManager
        from app.website_collections.manager import WebsiteCollectionManager
        from config.settings import get_settings

        _settings = get_settings()
        tool_connector_map = _build_tool_connector_map()
        if api_key_id is None:
            api_key_id = await _get_user_api_key_id(user_id, self._db)

        kbs = await KnowledgeBaseManager(self._db).list_kbs(user_id)
        wcs = await WebsiteCollectionManager(self._db).list_collections(user_id)
        kb_map = {kb.name: kb.id for kb in kbs}
        wc_map = {wc.name: wc.id for wc in wcs}

        agent_mgr = AgentManager(self._db)
        for order, spec in enumerate(agent_plan, start=1):
            tools_config = [
                {"tool": t, "connector_slug": tool_connector_map.get(t, t), "requires_hitl": False}
                for t in spec.get("tools", [])
            ]
            kb_ids = [kb_map[n] for n in spec.get("kb_names", []) if n in kb_map]
            collection_ids = [wc_map[n] for n in spec.get("wc_names", []) if n in wc_map]
            await agent_mgr.create_agent(
                workspace_id=workspace_id,
                user_id=user_id,
                name=spec.get("name", f"Agent {order}"),
                system_prompt=spec.get("system_prompt") or spec.get("role", ""),
                model_id=_settings.DEFAULT_MODEL,
                api_key_id=api_key_id,
                display_order=order,
                tools_config=tools_config,
                kb_ids=kb_ids or None,
                collection_ids=collection_ids or None,
            )

    async def toggle_job(self, job_id: UUID, user_id: UUID, is_active: bool) -> CronJob:
        job = await self.get_job(job_id, user_id)
        job.is_active = is_active
        if is_active:
            job.next_run_at = _compute_next_run(job.cron_expr)
        else:
            job.next_run_at = None
        await self._db.commit()
        await self._db.refresh(job)
        # Beat registration/deletion after commit only
        if is_active:
            self._register_beat(job)
        else:
            self._delete_beat(str(job_id))
        return job

    async def update_job(
        self,
        job_id: UUID,
        user_id: UUID,
        natural_query: str,
        cron_expr: str,
        human_schedule: str,
        timezone: str | None = None,
    ) -> CronJob:
        if not _croniter.is_valid(cron_expr):
            raise AppError("INVALID_CRON", f"Invalid cron expression: {cron_expr}", 400)

        job = await self.get_job(job_id, user_id)
        # Delete old beat before updating to avoid stale schedule
        self._delete_beat(str(job_id))
        job.natural_query = natural_query
        job.cron_expr = cron_expr
        job.human_schedule = human_schedule
        if timezone is not None:
            job.timezone = timezone
        job.next_run_at = _compute_next_run(cron_expr)
        await self._db.commit()
        await self._db.refresh(job)
        if job.is_active:
            self._register_beat(job)
        return job

    async def delete_job(self, job_id: UUID, user_id: UUID) -> None:
        job = await self.get_job(job_id, user_id)
        self._delete_beat(str(job_id))
        try:
            await WorkspaceManager(self._db).delete_workspace(job.workspace_id, user_id)
        except Exception as exc:
            logger.warning("Could not soft-delete workspace %s for cron job %s: %s",
                           job.workspace_id, job_id, exc)
        await self._db.delete(job)
        await self._db.commit()

    async def run_now(self, job_id: UUID, user_id: UUID) -> CronJob:
        from app.cron_jobs.tasks import run_cron_job_task
        job = await self.get_job(job_id, user_id)
        result = run_cron_job_task.delay(str(job_id))
        job.celery_task_id = result.id
        await self._db.commit()
        await self._db.refresh(job)
        return job

    def _register_beat(self, job: CronJob) -> None:
        parts = job.cron_expr.split()
        if len(parts) != 5:
            logger.warning("Invalid cron_expr for beat registration: %s", job.cron_expr)
            return
        try:
            entry = RedBeatSchedulerEntry(
                name=f"cron:{job.id}",
                task="app.cron_jobs.tasks.run_cron_job_task",
                schedule=crontab(
                    minute=parts[0],
                    hour=parts[1],
                    day_of_month=parts[2],
                    month_of_year=parts[3],
                    day_of_week=parts[4],
                ),
                args=[str(job.id)],
                app=celery_app,
            )
            entry.save()
        except Exception as exc:
            logger.error("Failed to register beat for cron job %s: %s", job.id, exc)

    def _delete_beat(self, job_id: str) -> None:
        try:
            entry = RedBeatSchedulerEntry.from_key(f"redbeat:cron:{job_id}", app=celery_app)
            entry.delete()
        except KeyError:
            pass  # entry doesn't exist — job was never activated
        except Exception as exc:
            logger.warning("Failed to delete beat entry for cron job %s: %s", job_id, exc)


def _compute_next_run(cron_expr: str) -> datetime | None:
    try:
        return _croniter(cron_expr, datetime.now(timezone.utc)).get_next(datetime)
    except Exception:
        return None


def _build_tool_connector_map() -> dict[str, str]:
    from tools.registry import get_registry
    registry = get_registry()
    return {s["name"]: s["connector"] for s in registry.get_tool_schemas(registry.all_tool_names())}


async def _get_user_api_key_id(user_id: UUID, db) -> UUID | None:
    from app.api_keys.manager import ApiKeyManager
    keys = await ApiKeyManager(db).list_keys(user_id)
    return keys[0].id if keys else None
