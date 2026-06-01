import asyncio
import logging
from datetime import datetime, timezone
from uuid import UUID

from celery_app import celery_app

logger = logging.getLogger(__name__)


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
    except Exception as exc:
        logger.error("run_cron_job_task retry: cron_job_id=%s attempt=%d err=%s",
                     cron_job_id, self.request.retries + 1, exc)
        raise self.retry(exc=exc)


async def _run_cron_job(cron_job_id: str) -> None:
    from database.session import get_custom_db_context_session
    from app.cron_jobs.db_models import CronJob
    from croniter import croniter

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

    # Run the workspace chat pipeline
    await _execute_workspace_run(workspace_id, user_id, query, tz)

    # Update last_run_at and next_run_at
    async with get_custom_db_context_session() as db:
        job = await db.get(CronJob, UUID(cron_job_id))
        if job:
            job.last_run_at = datetime.now(timezone.utc)
            try:
                it = croniter(cron_expr, datetime.now(timezone.utc))
                job.next_run_at = it.get_next(datetime)
            except Exception:
                job.next_run_at = None
            await db.commit()


async def _execute_workspace_run(
    workspace_id: UUID, user_id: UUID, query: str, timezone: str
) -> None:
    from database.session import get_custom_db_context_session
    from app.chat.manager import ChatManager
    from app.chat.db_models import MessageRoleEnum
    from app.chat.streaming import _load_workspace_context, _get_composer_key
    from app.api_keys.manager import ApiKeyManager
    from app.chat.db_models import Conversation
    from core.master_agent import generate_plan
    from core.orchestrator import OrchestrationContext, execute_plan
    from core.composer_agent import compose_response
    from core.memory_manager import MemoryManager
    from app.agents.db_models import AgentTypeEnum
    from config.settings import get_settings

    settings = get_settings()

    async with get_custom_db_context_session() as db:
        chat_mgr = ChatManager(db)
        conv = await chat_mgr.create_conversation(workspace_id, user_id)
        conversation_id = conv.id

        agents_db, api_keys_db, master_model, master_key, connector_tokens_db = (
            await _load_workspace_context(workspace_id, user_id, db)
        )
        summaries, recent_messages = await chat_mgr.load_memory_context(conversation_id)
        long_term_memory = await chat_mgr.get_long_term_memory(user_id)
        await chat_mgr.add_message(conversation_id, MessageRoleEnum.user, query)
        composer_model, composer_key = await _get_composer_key(workspace_id, user_id, db)

    if not master_key:
        logger.warning("Cron job workspace %s has no API key — skipping run", workspace_id)
        return

    mem_mgr = MemoryManager(conversation_id, model_id=master_model)
    mem_mgr.load(summaries, recent_messages)
    mem_mgr.add_message("user", query)

    planning_agents_db = {
        name: info for name, info in agents_db.items()
        if info.get("agent_type") == AgentTypeEnum.CUSTOM.value
    }

    try:
        plan = await generate_plan(
            query=query,
            agents_db=planning_agents_db,
            conversation_history=mem_mgr.get_window(),
            long_term_memory=long_term_memory,
            model_id=master_model,
            api_key=master_key,
            conversation_id=str(conversation_id),
            is_embed=True,
            timezone=timezone,
        )
    except Exception as exc:
        logger.error("Cron plan generation failed for workspace %s: %s", workspace_id, exc)
        return

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
        timezone=timezone,
    )

    def _auto_approve(_agent_id: str, _agent_name: str, _tool_names: list[str]):
        import asyncio
        future = asyncio.get_event_loop().create_future()
        future.set_result({"approved": True, "instructions": None, "request_id": "cron-auto"})
        return future

    agent_outputs = await execute_plan(plan, ctx, on_hitl_needed=_auto_approve)

    try:
        response_text, _artifacts, _suggestions, _usage = await compose_response(
            query=query,
            agent_outputs=agent_outputs,
            conversation_history=mem_mgr.get_window(),
            long_term_memory=long_term_memory,
            model_id=composer_model,
            api_key=composer_key,
            conversation_id=str(conversation_id),
            persona=None,
            timezone=timezone,
        )
        async with get_custom_db_context_session() as db:
            chat_mgr2 = ChatManager(db)
            await chat_mgr2.add_message(
                conversation_id, MessageRoleEnum.assistant, response_text
            )
    except Exception as exc:
        logger.error("Cron compose failed for workspace %s: %s", workspace_id, exc)
