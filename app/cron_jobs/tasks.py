import asyncio
import logging
from datetime import datetime, timezone
from uuid import UUID

from celery_app import celery_app

logger = logging.getLogger(__name__)


class _SkipRetry(Exception):
    """Non-retryable config error — retrying won't help."""


@celery_app.task(
    bind=True,
    max_retries=2,
    acks_late=True,
    soft_time_limit=600,
    time_limit=660,
    name="app.cron_jobs.tasks.run_cron_job_task",
)
def run_cron_job_task(self, cron_job_id: str) -> None:
    try:
        asyncio.run(_run_cron_job(cron_job_id))
    except _SkipRetry as exc:
        logger.warning("Cron job %s skipped (config issue): %s", cron_job_id, exc)
        try:
            asyncio.run(_update_job_timestamps(cron_job_id))
        except Exception as ts_exc:
            logger.error("Failed to update timestamps for %s: %s", cron_job_id, ts_exc)
    except Exception as exc:
        attempt = self.request.retries + 1
        logger.error(
            "run_cron_job_task failed: cron_job_id=%s attempt=%d/%d err=%s",
            cron_job_id, attempt, self.max_retries + 1, exc,
        )
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=60)
        try:
            asyncio.run(_update_job_timestamps(cron_job_id))
        except Exception as ts_exc:
            logger.error("Failed to update timestamps for %s after max retries: %s", cron_job_id, ts_exc)


async def _run_cron_job(cron_job_id: str) -> None:
    from database.session import get_custom_db_context_session
    from app.cron_jobs.db_models import CronJob

    async with get_custom_db_context_session() as db:
        job = await db.get(CronJob, UUID(cron_job_id))
        if not job or not job.is_active:
            logger.info("Cron job %s not found or inactive — skipping", cron_job_id)
            return

        workspace_id = job.workspace_id
        user_id = job.user_id
        query = job.natural_query
        tz = job.timezone
        cron_expr = job.cron_expr

    await _execute_workspace_run(workspace_id, user_id, query, tz)
    await _update_job_timestamps(cron_job_id, cron_expr)


async def _update_job_timestamps(cron_job_id: str, cron_expr: str | None = None) -> None:
    from database.session import get_custom_db_context_session
    from app.cron_jobs.db_models import CronJob
    from croniter import croniter

    async with get_custom_db_context_session() as db:
        job = await db.get(CronJob, UUID(cron_job_id))
        if not job:
            return
        expr = cron_expr or job.cron_expr
        job.last_run_at = datetime.now(timezone.utc)
        try:
            job.next_run_at = croniter(expr, datetime.now(timezone.utc)).get_next(datetime)
        except Exception:
            job.next_run_at = None


async def _execute_workspace_run(
    workspace_id: UUID, user_id: UUID, query: str, tz: str
) -> None:
    from database.session import get_custom_db_context_session
    from app.chat.manager import ChatManager
    from app.chat.db_models import MessageRoleEnum, Conversation  # noqa: F401
    from app.chat.streaming import load_workspace_context, get_composer_key
    from core.master_agent import generate_plan
    from core.orchestrator import OrchestrationContext, execute_plan
    from core.composer_agent import compose_response
    from core.memory_manager import MemoryManager
    from app.agents.db_models import AgentTypeEnum

    async with get_custom_db_context_session() as db:
        chat_mgr = ChatManager(db)
        conv = await chat_mgr.create_conversation(workspace_id, user_id)
        conversation_id = conv.id

        agents_db, api_keys_db, master_model, master_key, connector_tokens_db = (
            await load_workspace_context(workspace_id, user_id, db)
        )
        summaries, recent_messages = await chat_mgr.load_memory_context(conversation_id)
        long_term_memory = await chat_mgr.get_long_term_memory(user_id)
        await chat_mgr.add_message(conversation_id, MessageRoleEnum.user, query)
        composer_model, composer_key = await get_composer_key(workspace_id, user_id, db)

    if not master_key:
        from app.api_keys.manager import ApiKeyManager
        from app.connectors.encryption import decrypt_str
        from config.settings import get_settings
        _settings = get_settings()
        async with get_custom_db_context_session() as db:
            keys = await ApiKeyManager(db).list_keys(user_id)
        if not keys:
            raise _SkipRetry(f"No API key for workspace {workspace_id} — user must add one")
        master_key = decrypt_str(keys[0].encrypted_key)
        master_model = _settings.DEFAULT_MODEL

    if not composer_key:
        composer_key = master_key
        composer_model = master_model

    mem_mgr = MemoryManager(conversation_id, model_id=master_model)
    mem_mgr.load(summaries, recent_messages)
    mem_mgr.add_message("user", query)

    planning_agents_db = {
        name: info for name, info in agents_db.items()
        if info.get("agent_type") == AgentTypeEnum.CUSTOM.value
    }

    plan = await generate_plan(
        query=query,
        agents_db=planning_agents_db,
        conversation_history=mem_mgr.get_window(),
        long_term_memory=long_term_memory,
        model_id=master_model,
        api_key=master_key,
        conversation_id=str(conversation_id),
        is_embed=True,
        timezone=tz,
    )

    ctx = OrchestrationContext(
        user_id=user_id,
        conversation_id=conversation_id,
        workspace_id=workspace_id,
        conversation_history=mem_mgr.get_window(),
        long_term_memory=long_term_memory,
        agents_db=agents_db,
        api_keys_db=api_keys_db,
        connector_tokens_db=connector_tokens_db,
        persona=None,
        is_embed=True,
        timezone=tz,
    )

    def _auto_approve(_agent_id: str, _agent_name: str, _tool_names: list[str]):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        future.set_result({"approved": True, "instructions": None, "request_id": "cron-auto"})
        return future

    agent_outputs = await execute_plan(plan, ctx, on_hitl_needed=_auto_approve)

    response_text, _artifacts, _suggestions, _usage = await compose_response(
        query=query,
        agent_outputs=agent_outputs,
        conversation_history=mem_mgr.get_window(),
        long_term_memory=long_term_memory,
        model_id=composer_model,
        api_key=composer_key,
        conversation_id=str(conversation_id),
        persona=None,
        timezone=tz,
    )
    async with get_custom_db_context_session() as db:
        await ChatManager(db).add_message(
            conversation_id, MessageRoleEnum.assistant, response_text
        )
