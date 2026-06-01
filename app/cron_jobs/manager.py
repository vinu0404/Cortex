import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cron_jobs.db_models import CronJob
from app.common.exceptions import ForbiddenError, NotFoundError

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
        tools_needed: list[str],
    ) -> CronJob:
        from app.workspaces.manager import WorkspaceManager
        from database.session import get_custom_db_context_session

        # Create workspace as cron type with HITL auto-approve
        async with get_custom_db_context_session() as ws_db:
            ws_mgr = WorkspaceManager(ws_db)
            ws = await ws_mgr.create_workspace(user_id, name, task_description or natural_query, workspace_type="cron")
            ws.embed_hitl_auto_approve = True
            await ws_db.commit()
            workspace_id = ws.id

        job = CronJob(
            user_id=user_id,
            workspace_id=workspace_id,
            name=name,
            natural_query=natural_query,
            cron_expr=cron_expr,
            human_schedule=human_schedule,
            timezone=tz,
            is_active=True,
        )
        job.next_run_at = _compute_next_run(cron_expr)
        self._db.add(job)
        await self._db.flush()
        _register_beat(job)
        return job

    async def toggle_job(self, job_id: UUID, user_id: UUID, is_active: bool) -> CronJob:
        job = await self.get_job(job_id, user_id)
        job.is_active = is_active
        if is_active:
            job.next_run_at = _compute_next_run(job.cron_expr)
            _register_beat(job)
        else:
            job.next_run_at = None
            _delete_beat(str(job_id))
        await self._db.flush()
        return job

    async def update_job(
        self, job_id: UUID, user_id: UUID, natural_query: str, cron_expr: str, human_schedule: str
    ) -> CronJob:
        job = await self.get_job(job_id, user_id)
        _delete_beat(str(job_id))
        job.natural_query = natural_query
        job.cron_expr = cron_expr
        job.human_schedule = human_schedule
        job.next_run_at = _compute_next_run(cron_expr)
        if job.is_active:
            _register_beat(job)
        await self._db.flush()
        return job

    async def delete_job(self, job_id: UUID, user_id: UUID) -> None:
        job = await self.get_job(job_id, user_id)
        _delete_beat(str(job_id))
        # Soft-delete linked workspace
        from app.workspaces.manager import WorkspaceManager
        ws_mgr = WorkspaceManager(self._db)
        try:
            await ws_mgr.delete_workspace(job.workspace_id, user_id)
        except Exception:
            pass  # workspace may already be gone
        await self._db.delete(job)

    async def run_now(self, job_id: UUID, user_id: UUID) -> CronJob:
        job = await self.get_job(job_id, user_id)
        from app.cron_jobs.tasks import run_cron_job_task
        result = run_cron_job_task.delay(str(job_id))
        job.celery_task_id = result.id
        await self._db.flush()
        return job


def _compute_next_run(cron_expr: str) -> datetime | None:
    try:
        from croniter import croniter
        it = croniter(cron_expr, datetime.now(timezone.utc))
        return it.get_next(datetime)
    except Exception:
        return None


def _register_beat(job: CronJob) -> None:
    try:
        from celery.schedules import crontab
        from redbeat import RedBeatSchedulerEntry
        from celery_app import celery_app

        parts = job.cron_expr.split()
        if len(parts) != 5:
            logger.warning("Invalid cron_expr for beat registration: %s", job.cron_expr)
            return
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
        logger.error("Failed to register beat task for cron job %s: %s", job.id, exc)


def _delete_beat(job_id: str) -> None:
    try:
        from redbeat import RedBeatSchedulerEntry
        from celery_app import celery_app
        entry = RedBeatSchedulerEntry.from_key(f"redbeat:cron:{job_id}", app=celery_app)
        entry.delete()
    except Exception:
        pass  # entry may not exist; ignore
