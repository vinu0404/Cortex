"""SSE analysis stream for cron job builder. Mirrors app/vinu/builder.py pattern."""
import json
import logging
from collections.abc import AsyncGenerator

from app.auth.db_models import User
from config.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def analyze_cron_query(
    user: User,
    natural_query: str,
    timezone: str = "UTC",
) -> AsyncGenerator[str, None]:
    try:
        model_id, raw_key = await _resolve_api_key(user)

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
            agent_plan = await _plan_agents(natural_query, model_id, raw_key)
        except Exception as exc:
            yield _sse("error", {"message": f"Could not plan agents: {exc}"})
            return
        yield _sse("step", {
            "step": "Planning agent configuration", "status": "done",
            "result": {"agents": agent_plan["agents"], "tools_needed": agent_plan["tools_needed"]},
        })

        done_payload: dict = {
            "cron_expr": parsed["cron_expr"],
            "human_schedule": parsed["human_schedule"],
            "task_description": agent_plan["task_description"],
            "agents": agent_plan["agents"],
            "tools_needed": agent_plan["tools_needed"],
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
    import litellm
    from app.common.langfuse_client import get_compiled_prompt

    prompt = get_compiled_prompt("cron_schedule_parser", {
        "natural_query": natural_query,
        "timezone": timezone,
    })
    response = await litellm.acompletion(
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


async def _plan_agents(natural_query: str, model_id: str, api_key: str) -> dict:
    import litellm
    from app.common.langfuse_client import get_compiled_prompt

    prompt = get_compiled_prompt("cron_agent_planner", {"natural_query": natural_query})
    response = await litellm.acompletion(
        model=model_id,
        messages=[{"role": "user", "content": prompt}],
        api_key=api_key,
        response_format={"type": "json_object"},
        metadata={"trace_name": "cron_agent_planner"},
    )
    raw = response.choices[0].message.content or "{}"
    data = json.loads(raw)
    return {
        "task_description": data.get("task_description", natural_query),
        "agents": data.get("agents", []),
        "tools_needed": data.get("tools_needed", []),
        "missing_tools": data.get("missing_tools", []),
        "missing_tools_message": data.get("missing_tools_message"),
    }
