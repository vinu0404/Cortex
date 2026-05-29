"""SSE streaming generator for chat endpoint."""
import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from uuid import UUID

from cryptography.exceptions import InvalidTag
from fastapi import Request
from sqlalchemy import and_, select
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

from app.agents.db_models import Agent, AgentTypeEnum
from app.api_keys.manager import ApiKeyManager
from app.chat.db_models import MessageRoleEnum
from app.chat.manager import ChatManager
from app.common.exceptions import NotFoundError, PlanValidationError, TokenBudgetExceededError
from app.common.redis_client import get_async_redis
from app.common.token_budget import TokenBudgetService
from config.settings import get_settings
from core.composer_agent import compose_response
from core.master_agent import generate_plan
from core.memory_manager import MemoryManager, schedule_long_term_memory
from core.orchestrator import OrchestrationContext, execute_plan
from core.schemas import AgentOutput, ExecutionPlan
from core.title_generator import schedule_title_generation
from database.session import get_custom_db_context_session

settings = get_settings()
logger = logging.getLogger(__name__)

_DB_RETRY_EXCEPTIONS = (OperationalError, InterfaceError)
# Exceptions that indicate a key/token is permanently unreadable (not retriable)
_KEY_LOAD_EXCEPTIONS = (OperationalError, InterfaceError, NotFoundError, InvalidTag, ValueError)


def sse_event(data: str | dict, event: str | None = None) -> str:
    payload = json.dumps(data) if isinstance(data, dict) else data
    if event:
        return f"event: {event}\ndata: {payload}\n\n"
    return f"data: {payload}\n\n"


async def chat_stream(
    request: Request,
    workspace_id: UUID,
    conversation_id: UUID,
    query: str,
    user_id: UUID,
    persona_id: UUID | None,
) -> AsyncGenerator[str, None]:
    start_time = time.monotonic()

    try:
        await TokenBudgetService().check_budget(user_id)
    except TokenBudgetExceededError as e:
        yield sse_event({"message": e.message}, "error")
        return

    # Load all workspace context in one scoped session — released before any LLM call
    try:
        async with get_custom_db_context_session() as db:
            chat_mgr = ChatManager(db)
            agents_db, api_keys_db, master_model, master_key, connector_tokens_db = (
                await _load_workspace_context(workspace_id, user_id, db)
            )
            summaries, recent_messages = await chat_mgr.load_memory_context(conversation_id)
            long_term_memory = await chat_mgr.get_long_term_memory(user_id)
            persona_prompt = await _load_persona(persona_id, user_id, db) if persona_id else None
            composer_model, composer_key = await _get_composer_key(workspace_id, user_id, db)
            await chat_mgr.add_message(conversation_id, MessageRoleEnum.user, query)
    except _DB_RETRY_EXCEPTIONS as e:
        logger.error("DB error loading workspace context for %s: %s", workspace_id, e)
        yield sse_event({"message": "Database error while loading workspace"}, "error")
        return
    except Exception as e:
        logger.exception("Unexpected error loading workspace context for %s", workspace_id)
        yield sse_event({"message": f"Failed to load workspace: {e}"}, "error")
        return

    if not master_key:
        yield sse_event({"message": "No API key configured for Master agent"}, "error")
        return

    mem_mgr = MemoryManager(conversation_id)
    mem_mgr.load(summaries, recent_messages)
    mem_mgr.add_message("user", query)

    if await request.is_disconnected():
        return

    # Only CUSTOM agents are shown to the Master LLM; system agents excluded from plan
    planning_agents_db = {
        name: info for name, info in agents_db.items()
        if info.get("agent_type") == AgentTypeEnum.CUSTOM.value
    }

    # --- Planning ---
    yield sse_event({"phase": "planning", "agent_name": "Master"}, "status")

    try:
        plan = await generate_plan(
            query=query,
            agents_db=planning_agents_db,
            conversation_history=mem_mgr.get_window(),
            long_term_memory=long_term_memory,
            model_id=master_model,
            api_key=master_key,
            conversation_id=str(conversation_id),
        )
    except PlanValidationError as e:
        yield sse_event({"message": str(e)}, "error")
        return

    yield sse_event({"execution_order": _build_plan_string(plan)}, "plan")

    if await request.is_disconnected():
        return

    # --- Execution ---
    ctx = OrchestrationContext(
        user_id=user_id,
        conversation_id=conversation_id,
        workspace_id=workspace_id,
        conversation_history=mem_mgr.get_window(),
        long_term_memory=long_term_memory,
        agents_db=agents_db,
        api_keys_db=api_keys_db,
        connector_tokens_db=connector_tokens_db,
        persona=persona_prompt,
    )

    hitl_futures: dict[str, asyncio.Future] = {}

    def on_hitl_needed(agent_id: str, agent_name: str, tool_names: list[str]) -> asyncio.Future:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        hitl_futures[agent_id] = future
        return future

    for step in plan.steps:
        yield sse_event({"phase": "executing", "agent_name": step.agent_name}, "status")

    execute_task = asyncio.create_task(execute_plan(plan, ctx, on_hitl_needed))

    # HITL state: one DB record + one SSE event per agent_id, no flooding
    hitl_wait_tasks: dict[str, asyncio.Task] = {}
    hitl_req_ids: dict[str, str] = {}

    while not execute_task.done():
        if await request.is_disconnected():
            execute_task.cancel()
            return

        for agent_id, future in list(hitl_futures.items()):
            if future.done():
                hitl_wait_tasks.pop(agent_id, None)
                hitl_req_ids.pop(agent_id, None)
                continue

            if agent_id not in hitl_wait_tasks:
                # First encounter — create DB record and emit SSE exactly once
                hitl_req = await _create_hitl_record(agent_id, plan, conversation_id)
                req_id_str = str(hitl_req.id)
                hitl_req_ids[agent_id] = req_id_str
                # Start wait task — subscribes to Redis in background
                hitl_wait_tasks[agent_id] = asyncio.create_task(_wait_for_hitl_decision(req_id_str))
                yield sse_event({
                    "request_id": req_id_str,
                    "agent_name": _get_step_name(agent_id, plan),
                    "tool_names": hitl_req.tool_names,
                    "timeout_seconds": settings.HITL_TIMEOUT_SECONDS,
                }, "hitl_required")

            elif hitl_wait_tasks[agent_id].done():
                # Decision arrived — resolve future and emit result SSE
                req_id_str = hitl_req_ids[agent_id]
                try:
                    decision = hitl_wait_tasks[agent_id].result()
                except asyncio.CancelledError:
                    decision = {"approved": False, "instructions": None}
                except Exception:
                    logger.exception("HITL wait task failed for agent %s — auto-denying", agent_id)
                    decision = {"approved": False, "instructions": None}

                if decision.get("approved"):
                    yield sse_event({"request_id": req_id_str, "instructions": decision.get("instructions")}, "hitl_approved")
                else:
                    yield sse_event({"request_id": req_id_str}, "hitl_denied")

                future.set_result({"request_id": req_id_str, **decision})
                hitl_wait_tasks.pop(agent_id, None)
                hitl_req_ids.pop(agent_id, None)

        await asyncio.sleep(0.05)

    try:
        agent_outputs: dict[str, AgentOutput] = execute_task.result()
    except Exception as e:
        yield sse_event({"message": f"Execution error: {e}"}, "error")
        return

    # --- Memory compression ---
    if mem_mgr.needs_compression():
        yield sse_event({"message": "Summarising earlier conversation..."}, "compacting")
        summary = await mem_mgr.compress(master_model, master_key)
        if summary:
            async with get_custom_db_context_session() as db:
                await ChatManager(db).save_summary(
                    conversation_id, summary, 0, settings.SHORT_TERM_COMPRESS_FIRST_N
                )

    if await request.is_disconnected():
        return

    # --- Composing ---
    yield sse_event({"phase": "composing", "agent_name": "Composer"}, "status")

    try:
        response_text, artifacts, suggestions, composer_tokens = await compose_response(
            query=query,
            agent_outputs=agent_outputs,
            conversation_history=mem_mgr.get_window(),
            long_term_memory=long_term_memory,
            model_id=composer_model,
            api_key=composer_key,
            conversation_id=str(conversation_id),
            persona=persona_prompt,
        )
    except Exception:
        logger.exception("Composer failed for conversation %s", conversation_id)
        yield sse_event({"message": "Composer failed — please retry"}, "error")
        return

    for char in response_text:
        if await request.is_disconnected():
            return
        yield sse_event({"text": char}, "token")

    for artifact in artifacts:
        yield sse_event(artifact.model_dump(), "artifact")

    if suggestions:
        yield sse_event({"questions": suggestions}, "suggestions")

    elapsed_ms = int((time.monotonic() - start_time) * 1000)
    async with get_custom_db_context_session() as db:
        await ChatManager(db).add_message(
            conversation_id, MessageRoleEnum.assistant, response_text, latency_ms=elapsed_ms
        )
    mem_mgr.add_message("assistant", response_text)

    # Record token usage for budget enforcement (fire-and-forget)
    agent_tokens = sum(o.resource_usage.get("tokens_used", 0) for o in agent_outputs.values())
    total_tokens = agent_tokens + composer_tokens
    if total_tokens > 0:
        asyncio.create_task(TokenBudgetService().record_usage(user_id, total_tokens))

    async def _update_title(conv_id: UUID, title: str) -> None:
        async with get_custom_db_context_session() as bg_db:
            await ChatManager(bg_db).update_conversation_title(conv_id, title)

    async def _update_ltm(uid: UUID, facts: dict, prefs: dict) -> None:
        async with get_custom_db_context_session() as bg_db:
            await ChatManager(bg_db).upsert_long_term_memory(uid, facts, prefs)

    if not recent_messages:
        schedule_title_generation(
            query, response_text, conversation_id, master_model, master_key, _update_title
        )
    schedule_long_term_memory(query, response_text, user_id, master_model, master_key, _update_ltm)

    yield sse_event({"total_ms": elapsed_ms, "conversation_id": str(conversation_id)}, "done")


# ---- Helpers ----

def _build_plan_string(plan: ExecutionPlan) -> str:
    parts = ["Master"]
    for step in plan.steps:
        tools_str = f"[{','.join(step.tools)}]" if step.tools else ""
        parts.append(f"{step.agent_name}{tools_str}")
    parts.append("Composer")
    return " → ".join(parts)


def _get_step_name(agent_id: str, plan: ExecutionPlan) -> str:
    for step in plan.steps:
        if step.agent_id == agent_id:
            return step.agent_name
    return agent_id


async def _create_hitl_record(
    agent_id: str, plan: ExecutionPlan, conversation_id: UUID
):
    tool_names = next(
        (step.tools for step in plan.steps if step.agent_id == agent_id), []
    )
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.HITL_TIMEOUT_SECONDS)
    async with get_custom_db_context_session() as db:
        return await ChatManager(db).create_hitl_request(conversation_id, agent_id, tool_names, expires_at)


async def _wait_for_hitl_decision(request_id: str) -> dict:
    redis = get_async_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"hitl:{request_id}")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.HITL_TIMEOUT_SECONDS
    try:
        while loop.time() < deadline:
            try:
                msg = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                msg = None
            if msg and msg["type"] == "message":
                return json.loads(msg["data"])
        return {"approved": False, "instructions": None}
    finally:
        await pubsub.unsubscribe(f"hitl:{request_id}")
        await pubsub.aclose()


@retry(
    stop=stop_after_attempt(settings.REDIS_MAX_RETRIES),
    wait=wait_fixed(settings.REDIS_RETRY_WAIT_FIXED),
    retry=retry_if_exception_type(_DB_RETRY_EXCEPTIONS),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
async def _fetch_api_key(key_mgr: ApiKeyManager, key_id: UUID, user_id: UUID) -> str:
    return await key_mgr.get_decrypted_key(key_id, user_id)


@retry(
    stop=stop_after_attempt(settings.REDIS_MAX_RETRIES),
    wait=wait_fixed(settings.REDIS_RETRY_WAIT_FIXED),
    retry=retry_if_exception_type(_DB_RETRY_EXCEPTIONS),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
async def _fetch_connector_instances(user_id: UUID, db: AsyncSession) -> list:
    from app.connectors.db_models import ConnectorInstance
    return list(await db.scalars(
        select(ConnectorInstance)
        .where(ConnectorInstance.user_id == user_id)
        .options(selectinload(ConnectorInstance.definition))
    ))


async def _load_workspace_context(
    workspace_id: UUID, user_id: UUID, db: AsyncSession
) -> tuple[dict, dict, str, str, dict]:
    from app.connectors.encryption import decrypt_json

    agents = list(await db.scalars(
        select(Agent).where(
            and_(Agent.workspace_id == workspace_id, Agent.deleted_at.is_(None))
        )
    ))

    key_mgr = ApiKeyManager(db)
    api_keys_db: dict = {}
    agents_db: dict = {}
    master_model = settings.DEFAULT_MODEL
    master_key = ""

    # Load KB + website collection assignments for all agents in one query each
    from app.knowledge_bases.db_models import AgentKnowledgeBase
    from app.website_collections.db_models import AgentWebsiteCollection
    agent_ids = [a.id for a in agents]
    kb_rows = list(await db.scalars(
        select(AgentKnowledgeBase).where(AgentKnowledgeBase.agent_id.in_(agent_ids))
    )) if agent_ids else []
    kb_map: dict = {}
    for row in kb_rows:
        kb_map.setdefault(row.agent_id, []).append(str(row.kb_id))

    wc_rows = list(await db.scalars(
        select(AgentWebsiteCollection).where(AgentWebsiteCollection.agent_id.in_(agent_ids))
    )) if agent_ids else []
    wc_map: dict = {}
    for row in wc_rows:
        wc_map.setdefault(row.agent_id, []).append(str(row.collection_id))

    for agent in agents:
        agents_db[agent.name] = {
            "id": str(agent.id),
            "system_prompt": agent.system_prompt or "",
            "model_id": agent.model_id or settings.DEFAULT_MODEL,
            "api_key_id": agent.api_key_id,
            "tools_config": agent.tools_config or [],
            "agent_type": agent.agent_type.value,
            "kb_ids": kb_map.get(agent.id, []),
            "collection_ids": wc_map.get(agent.id, []),
        }
        if agent.api_key_id and agent.api_key_id not in api_keys_db:
            try:
                api_keys_db[agent.api_key_id] = await _fetch_api_key(key_mgr, agent.api_key_id, user_id)
            except _KEY_LOAD_EXCEPTIONS as e:
                # Key unreadable after retries — agent will run keyless (logged, non-fatal)
                logger.error(
                    "Key %s for agent '%s' unreadable (%s) — agent will run without key",
                    agent.api_key_id, agent.name, type(e).__name__,
                )

        if agent.agent_type == AgentTypeEnum.MASTER and agent.api_key_id:
            master_model = agent.model_id or settings.DEFAULT_MODEL
            master_key = api_keys_db.get(agent.api_key_id, "")

    # Load OAuth tokens for connector tool injection
    connector_tokens_db: dict[str, dict] = {}
    instances = await _fetch_connector_instances(user_id, db)
    for inst in instances:
        try:
            connector_tokens_db[inst.definition.slug] = decrypt_json(inst.encrypted_tokens)
        except (InvalidTag, ValueError) as e:
            # Corrupted/wrong-key token — log and skip this connector
            logger.error(
                "Connector '%s' tokens undecryptable (%s) — tools for this connector unavailable",
                inst.definition.slug, type(e).__name__,
            )

    return agents_db, api_keys_db, master_model, master_key, connector_tokens_db


@retry(
    stop=stop_after_attempt(settings.REDIS_MAX_RETRIES),
    wait=wait_fixed(settings.REDIS_RETRY_WAIT_FIXED),
    retry=retry_if_exception_type(_DB_RETRY_EXCEPTIONS),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=False,
)
async def _get_composer_key(workspace_id: UUID, user_id: UUID, db: AsyncSession) -> tuple[str, str]:
    composer = await db.scalar(
        select(Agent).where(and_(
            Agent.workspace_id == workspace_id,
            Agent.agent_type == AgentTypeEnum.COMPOSER,
            Agent.deleted_at.is_(None),
        ))
    )
    if not composer or not composer.api_key_id:
        return settings.DEFAULT_MODEL, ""

    key = await _fetch_api_key(ApiKeyManager(db), composer.api_key_id, user_id)
    return composer.model_id or settings.DEFAULT_MODEL, key


async def _load_persona(persona_id: UUID, user_id: UUID, db: AsyncSession) -> str | None:
    from app.personas.db_models import Persona
    persona = await db.get(Persona, persona_id)
    if not persona or persona.user_id != user_id:
        return None
    return persona.system_prompt
