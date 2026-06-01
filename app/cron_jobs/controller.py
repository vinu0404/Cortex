import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.db_models import User
from app.common.api_response import fail, ok
from app.common.exceptions import AppError
from app.cron_jobs.builder import analyze_cron_query, refine_cron_plan
from app.cron_jobs.manager import CronJobManager
from app.cron_jobs.models import CronJobCreate, CronJobResponse, CronJobUpdate, ParseScheduleRequest, RefinePlanRequest, ToggleJobRequest
from database.session import get_db

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/analyze")
async def analyze(
    body: ParseScheduleRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    tz = request.headers.get("X-Timezone", body.timezone or "UTC")

    async def _stream():
        async for chunk in analyze_cron_query(current_user, body.natural_query, tz):
            yield chunk

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/analyze/refine", response_model=None)
async def refine_plan(
    body: RefinePlanRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> JSONResponse:
    try:
        tz = request.headers.get("X-Timezone", body.timezone or "UTC")
        result = await refine_cron_plan(
            user=current_user,
            natural_query=body.natural_query,
            current_agents=body.current_agents,
            change_request=body.change_request,
            timezone=tz,
        )
        return ok(result)
    except Exception as e:
        logger.error("refine_plan failed: %s", e, exc_info=True)
        return fail("INTERNAL_ERROR", "Failed to refine plan", 500)


@router.post("", response_model=None)
async def create_job(
    body: CronJobCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        tz = request.headers.get("X-Timezone", body.timezone or "UTC")
        job = await CronJobManager(db).create_job(
            user_id=current_user.id,
            name=body.name,
            natural_query=body.natural_query,
            cron_expr=body.cron_expr,
            human_schedule=body.human_schedule,
            tz=tz,
            task_description=body.task_description,
            agent_plan=[a.model_dump() for a in body.agents],
        )
        return ok(CronJobResponse.model_validate(job).model_dump(mode="json"), status_code=201)
    except AppError as e:
        return fail(e.code, e.message, e.status_code)
    except Exception as e:
        logger.error("create_job failed: %s", e, exc_info=True)
        return fail("INTERNAL_ERROR", "Failed to create cron job", 500)


@router.get("", response_model=None)
async def list_jobs(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        jobs = await CronJobManager(db).list_jobs(current_user.id)
        return ok([CronJobResponse.model_validate(j).model_dump(mode="json") for j in jobs])
    except AppError as e:
        return fail(e.code, e.message, e.status_code)
    except Exception as e:
        logger.error("list_jobs failed: %s", e, exc_info=True)
        return fail("INTERNAL_ERROR", "Failed to list cron jobs", 500)


@router.get("/{job_id}", response_model=None)
async def get_job(
    job_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        job = await CronJobManager(db).get_job(job_id, current_user.id)
        return ok(CronJobResponse.model_validate(job).model_dump(mode="json"))
    except AppError as e:
        return fail(e.code, e.message, e.status_code)
    except Exception as e:
        logger.error("get_job failed: %s", e, exc_info=True)
        return fail("INTERNAL_ERROR", "Failed to get cron job", 500)


@router.post("/{job_id}/run-now", response_model=None)
async def run_now(
    job_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        job = await CronJobManager(db).run_now(job_id, current_user.id)
        return ok({"message": "Cron job dispatched", "celery_task_id": job.celery_task_id})
    except AppError as e:
        return fail(e.code, e.message, e.status_code)
    except Exception as e:
        logger.error("run_now failed: %s", e, exc_info=True)
        return fail("INTERNAL_ERROR", "Failed to dispatch cron job", 500)


@router.patch("/{job_id}", response_model=None)
async def update_job(
    job_id: UUID,
    body: CronJobUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        job = await CronJobManager(db).update_job(
            job_id, current_user.id, body.natural_query, body.cron_expr, body.human_schedule, body.timezone
        )
        return ok(CronJobResponse.model_validate(job).model_dump(mode="json"))
    except AppError as e:
        return fail(e.code, e.message, e.status_code)
    except Exception as e:
        logger.error("update_job failed: %s", e, exc_info=True)
        return fail("INTERNAL_ERROR", "Failed to update cron job", 500)


@router.patch("/{job_id}/toggle", response_model=None)
async def toggle_job(
    job_id: UUID,
    body: ToggleJobRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        job = await CronJobManager(db).toggle_job(job_id, current_user.id, body.is_active)
        return ok(CronJobResponse.model_validate(job).model_dump(mode="json"))
    except AppError as e:
        return fail(e.code, e.message, e.status_code)
    except Exception as e:
        logger.error("toggle_job failed: %s", e, exc_info=True)
        return fail("INTERNAL_ERROR", "Failed to toggle cron job", 500)


@router.delete("/{job_id}", response_model=None)
async def delete_job(
    job_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        await CronJobManager(db).delete_job(job_id, current_user.id)
        return ok(message="Cron job deleted")
    except AppError as e:
        return fail(e.code, e.message, e.status_code)
    except Exception as e:
        logger.error("delete_job failed: %s", e, exc_info=True)
        return fail("INTERNAL_ERROR", "Failed to delete cron job", 500)
