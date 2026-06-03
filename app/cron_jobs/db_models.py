import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from database.session import Base


class CronJob(Base):
    __tablename__ = "cron_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    natural_query: Mapped[str] = mapped_column(Text, nullable=False)
    cron_expr: Mapped[str] = mapped_column(String(100), nullable=False)
    human_schedule: Mapped[str] = mapped_column(String(255), nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False, default="UTC", server_default="UTC")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    task_description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    agent_plan: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default="[]")
    celery_task_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class CronJobModelService:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def list_jobs(self, user_id: uuid.UUID) -> list[CronJob]:
        result = await self._db.scalars(
            select(CronJob)
            .where(CronJob.user_id == user_id)
            .order_by(CronJob.created_at.desc())
        )
        return list(result)

    async def get_job(self, job_id: uuid.UUID) -> CronJob | None:
        return await self._db.get(CronJob, job_id)

    async def create_job(
        self,
        *,
        user_id: uuid.UUID,
        workspace_id: uuid.UUID,
        name: str,
        natural_query: str,
        cron_expr: str,
        human_schedule: str,
        timezone_name: str,
        task_description: str,
        agent_plan: list[dict],
        next_run_at: datetime | None,
    ) -> CronJob:
        job = CronJob(
            user_id=user_id,
            workspace_id=workspace_id,
            name=name,
            natural_query=natural_query,
            cron_expr=cron_expr,
            human_schedule=human_schedule,
            timezone=timezone_name,
            is_active=True,
            task_description=task_description,
            agent_plan=agent_plan,
            next_run_at=next_run_at,
        )
        self._db.add(job)
        await self._db.flush()
        return job

    async def set_job_active(self, job: CronJob, is_active: bool, next_run_at: datetime | None) -> CronJob:
        job.is_active = is_active
        job.next_run_at = next_run_at
        return job

    async def update_job_schedule(
        self,
        job: CronJob,
        natural_query: str,
        cron_expr: str,
        human_schedule: str,
        timezone_name: str | None,
        next_run_at: datetime | None,
    ) -> CronJob:
        job.natural_query = natural_query
        job.cron_expr = cron_expr
        job.human_schedule = human_schedule
        if timezone_name is not None:
            job.timezone = timezone_name
        job.next_run_at = next_run_at
        return job

    async def set_celery_task_id(self, job: CronJob, celery_task_id: str) -> CronJob:
        job.celery_task_id = celery_task_id
        return job

    async def delete_job(self, job: CronJob) -> None:
        await self._db.delete(job)

    async def save_changes(self) -> None:
        await self._db.commit()

    async def refresh(self, instance) -> None:
        await self._db.refresh(instance)
