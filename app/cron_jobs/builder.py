"""SSE analysis stream for cron job builder. Mirrors app/vinu/builder.py pattern."""
import json
import logging
from collections.abc import AsyncGenerator

from app.auth.db_models import User
from app.connectors.manager import get_auth_free_connector_slugs, get_connector_display
from app.common.retry import acompletion_with_retry
from config.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _collect_connectors_needed(agent_plan: list[dict], tool_connector_map: dict[str, str]) -> list[dict]:
    auth_free = get_auth_free_connector_slugs()
    seen: set[str] = set()
    for agent_spec in agent_plan:
        for tool_name in agent_spec.get("tools", []):
            slug = tool_connector_map.get(tool_name, "")
            if slug and slug not in auth_free:
                seen.add(slug)
    return [{"slug": slug, **get_connector_display(slug)} for slug in sorted(seen)]


async def analyze_cron_query(
    user: User,
    natural_query: str,
    timezone: str = "UTC",
) -> AsyncGenerator[str, None]:
    try:
        model_id, raw_key = await _resolve_api_key(user)

        if settings.GUARDRAILS_ENABLED:
            from app.common.guardrails import BLOCKED_INPUT_MESSAGE, check_input
            guard = await check_input(natural_query, raw_key)
            if guard.blocked:
                logger.warning("Cron input guardrail blocked (category=%s)", guard.category)
                yield _sse("error", {"message": BLOCKED_INPUT_MESSAGE})
                return

        yield _sse("step", {"step": "Detecting schedule", "status": "running"})
        try:
            parsed = await _parse_schedule(natural_query, timezone, model_id, raw_key)
        except Exception as exc:
            yield _sse("error", {"message": f"Could not parse schedule: {exc}"})
            return
        yield _sse("step", {
            "step": "Detecting schedule", "status": "done",
            "result": {"cron_expr": parsed["cron_expr"], "human_schedule": parsed["human_schedule"]},
        })

        yield _sse("step", {"step": "Planning agent configuration", "status": "running"})
        try:
            available_tools, kb_context, wc_context = await _load_user_resources(user.id)
            agent_plan = await _plan_agents(natural_query, model_id, raw_key,
                                            available_tools, kb_context, wc_context)
        except Exception as exc:
            yield _sse("error", {"message": f"Could not plan agents: {exc}"})
            return
        yield _sse("step", {
            "step": "Planning agent configuration", "status": "done",
            "result": {"agents": agent_plan["agents"], "tools_needed": agent_plan["tools_needed"]},
        })

        from app.cron_jobs.manager import _build_tool_connector_map
        tool_connector_map = _build_tool_connector_map()
        connectors_needed = _collect_connectors_needed(agent_plan["agents"], tool_connector_map)

        done_payload: dict = {
            "cron_expr": parsed["cron_expr"],
            "human_schedule": parsed["human_schedule"],
            "task_description": agent_plan["task_description"],
            "agents": agent_plan["agents"],
            "tools_needed": agent_plan["tools_needed"],
            "connectors_needed": connectors_needed,
        }
        if agent_plan.get("missing_tools"):
            done_payload["missing_tools"] = agent_plan["missing_tools"]
            done_payload["missing_tools_message"] = agent_plan.get("missing_tools_message", "")
        yield _sse("done", done_payload)

    except Exception as exc:
        logger.error("Cron builder failed: %s", exc, exc_info=True)
        yield _sse("error", {"message": str(exc)})


async def _resolve_api_key(user: User) -> tuple[str, str]:
    from app.api_keys.manager import ApiKeyManager
    from app.connectors.encryption import decrypt_str
    from database.session import get_custom_db_context_session

    async with get_custom_db_context_session() as db:
        keys = await ApiKeyManager(db).list_keys(user.id)
        for key in keys:
            if key.available_models:
                return key.available_models[0], decrypt_str(key.encrypted_key)
    return settings.DEFAULT_MODEL, ""


async def _parse_schedule(natural_query: str, timezone: str, model_id: str, api_key: str) -> dict:
    from app.common.langfuse_client import get_compiled_prompt

    prompt = get_compiled_prompt("cron_schedule_parser", {
        "natural_query": natural_query,
        "timezone": timezone,
    })
    response = await acompletion_with_retry(
        model=model_id,
        messages=[{"role": "user", "content": prompt}],
        api_key=api_key,
        response_format={"type": "json_object"},
        metadata={"trace_name": "cron_schedule_parser"},
    )
    raw = response.choices[0].message.content or "{}"
    data = json.loads(raw)
    return {
        "cron_expr": data.get("cron_expr", "0 9 * * *"),
        "human_schedule": data.get("human_schedule", "Daily at 9 AM"),
    }


async def _load_user_resources(user_id) -> tuple[str, str, str]:
    from tools.registry import get_registry
    from app.knowledge_bases.manager import KnowledgeBaseManager
    from app.website_collections.manager import WebsiteCollectionManager
    from database.session import get_custom_db_context_session

    registry = get_registry()
    schemas = registry.get_tool_schemas(registry.all_tool_names())
    available_tools = "\n".join(
        f"- {s['name']} (connector: {s['connector']}): {s['description']}"
        for s in schemas
    ) or "No tools available."

    try:
        async with get_custom_db_context_session() as db:
            kbs = await KnowledgeBaseManager(db).list_kbs(user_id)
            wcs = await WebsiteCollectionManager(db).list_collections(user_id)
        kb_context = "\n".join(
            f"- {kb.name}: {kb.description or 'No description'}"
            for kb in kbs
        ) or "None"
        wc_context = "\n".join(
            f"- {wc.name}: {wc.description or 'No description'}"
            for wc in wcs
        ) or "None"
    except Exception as exc:
        logger.warning("Could not load user KB/WC resources: %s", exc)
        kb_context = "None"
        wc_context = "None"

    tools_lines = [line.lstrip("- ") for line in available_tools.split("\n") if line.strip()]
    try:
        from app.mcp_servers.db_models import MCPServer
        from sqlalchemy import select as sa_select
        async with get_custom_db_context_session() as db:
            mcp_servers = list(await db.scalars(
                sa_select(MCPServer).where(MCPServer.user_id == user_id, MCPServer.is_active.is_(True))
            ))
        for srv in mcp_servers:
            for t in (srv.discovered_tools or []):
                tools_lines.append(
                    f"{t['name']} (connector: mcp:{srv.id}): {t.get('description', '')} [server: {srv.name}]"
                )
    except Exception as exc:
        logger.warning("Could not load MCP server tools: %s", exc)

    available_tools = "\n".join(f"- {line}" for line in tools_lines) or "No tools available."
    return available_tools, kb_context, wc_context


async def _plan_agents(
    natural_query: str,
    model_id: str,
    api_key: str,
    available_tools: str,
    kb_context: str,
    wc_context: str,
) -> dict:
    from app.common.langfuse_client import get_compiled_prompt

    prompt = get_compiled_prompt("cron_agent_planner", {
        "natural_query": natural_query,
        "available_tools": available_tools,
        "knowledge_bases": kb_context,
        "website_collections": wc_context,
    })
    logger.debug("cron_agent_planner prompt (first 800 chars): %.800s", prompt)
    response = await acompletion_with_retry(
        model=model_id,
        messages=[{"role": "user", "content": prompt}],
        api_key=api_key,
        response_format={"type": "json_object"},
        metadata={"trace_name": "cron_agent_planner"},
    )
    raw = response.choices[0].message.content or "{}"
    logger.debug("cron_agent_planner raw response: %s", raw)
    data = json.loads(raw)
    return {
        "task_description": data.get("task_description", natural_query),
        "agents": data.get("agents", []),
        "tools_needed": data.get("tools_needed", []),
        "missing_tools": data.get("missing_tools", []),
        "missing_tools_message": data.get("missing_tools_message"),
    }


async def refine_cron_plan(
    user: User,
    natural_query: str,
    current_agents: list[dict],
    change_request: str,
    timezone: str = "UTC",
) -> dict:
    from app.common.langfuse_client import get_compiled_prompt
    from app.cron_jobs.manager import _build_tool_connector_map

    model_id, raw_key = await _resolve_api_key(user)

    if settings.GUARDRAILS_ENABLED:
        from app.common.exceptions import AppError
        from app.common.guardrails import BLOCKED_INPUT_MESSAGE, check_input
        guard = await check_input(change_request, raw_key)
        if guard.blocked:
            logger.warning("Cron refine guardrail blocked (category=%s)", guard.category)
            raise AppError("GUARDRAIL_BLOCKED", BLOCKED_INPUT_MESSAGE, 400)

    available_tools, kb_context, wc_context = await _load_user_resources(user.id)

    prompt = get_compiled_prompt("cron_plan_refiner", {
        "natural_query": natural_query,
        "current_agents": json.dumps(current_agents, indent=2),
        "change_request": change_request,
        "available_tools": available_tools,
        "knowledge_bases": kb_context,
        "website_collections": wc_context,
    }, timezone)
    logger.debug("cron_plan_refiner prompt (first 800 chars): %.800s", prompt)
    response = await acompletion_with_retry(
        model=model_id,
        messages=[{"role": "user", "content": prompt}],
        api_key=raw_key,
        response_format={"type": "json_object"},
        metadata={"trace_name": "cron_plan_refiner"},
    )
    raw = response.choices[0].message.content or "{}"
    logger.debug("cron_plan_refiner raw response: %s", raw)
    data = json.loads(raw)

    tool_connector_map = _build_tool_connector_map()
    connectors_needed = _collect_connectors_needed(data.get("agents", []), tool_connector_map)

    return {
        "task_description": data.get("task_description", natural_query),
        "agents": data.get("agents", []),
        "tools_needed": data.get("tools_needed", []),
        "missing_tools": data.get("missing_tools", []),
        "missing_tools_message": data.get("missing_tools_message"),
        "connectors_needed": connectors_needed,
    }
