import json
import logging
from typing import Any

import litellm
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from config.settings import get_settings
from core.schemas import AgentInput, AgentOutput
from tools.registry import get_registry

settings = get_settings()
logger = logging.getLogger(__name__)


def _is_retriable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "rate" in msg or "timeout" in msg or "connection" in msg or "500" in msg or "503" in msg


def _build_system_prompt(agent_def: dict, agent_input: AgentInput) -> str:
    parts = []
    if agent_def.get("system_prompt"):
        parts.append(agent_def["system_prompt"])

    persona = agent_input.metadata.get("persona")
    if persona:
        parts.append(f"\n## Persona\n{persona}")

    if agent_input.long_term_memory.critical_facts:
        parts.append(f"\n## User Context\n{json.dumps(agent_input.long_term_memory.critical_facts)}")

    if agent_input.dependency_outputs:
        parts.append("\n## Outputs from upstream agents:")
        for dep_id, data in agent_input.dependency_outputs.items():
            parts.append(f"- {dep_id}: {json.dumps(data)[:500]}")

    if agent_input.hitl_context and agent_input.hitl_context.approved:
        if agent_input.hitl_context.instructions:
            parts.append(f"\n## Human Instructions for Tool Use\n{agent_input.hitl_context.instructions}")

    return "\n".join(parts)


_PY_TO_JSON: dict = {int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}


def _py_type_to_json(annotation: Any) -> str:
    import inspect as _inspect
    if annotation is _inspect.Parameter.empty:
        return "string"
    origin = getattr(annotation, "__origin__", None)
    if origin is list:
        return "array"
    if origin is dict:
        return "object"
    return _PY_TO_JSON.get(annotation, "string")


def _build_tool_schemas(tool_names: list[str]) -> list[dict]:
    import inspect
    registry = get_registry()
    schemas = []
    for name in tool_names:
        fn = registry.get_callable(name)
        if not fn:
            continue
        sig = inspect.signature(fn)
        props = {}
        required = []
        for param_name, param in sig.parameters.items():
            if param_name in ("access_token", "instance_url"):
                continue
            if param.default is inspect.Parameter.empty:
                required.append(param_name)
            props[param_name] = {
                "type": _py_type_to_json(param.annotation),
                "description": f"{param_name} parameter",
            }
        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": getattr(fn, "tool_description", ""),
                "parameters": {"type": "object", "properties": props, "required": required},
            },
        })
    return schemas


async def run_dynamic_agent(
    agent_input: AgentInput,
    agent_def: dict,
    model_id: str,
    api_key: str,
    connector_tokens_db: dict[str, dict] | None = None,
) -> AgentOutput:
    if agent_input.hitl_context and not agent_input.hitl_context.approved:
        return AgentOutput(
            agent_id=agent_input.agent_id,
            agent_name=agent_input.agent_name,
            task_description=agent_input.task,
            task_done=False,
            error="HITL denied by user",
        )

    tool_schemas = _build_tool_schemas(agent_input.tools) if agent_input.tools else []
    system_prompt = _build_system_prompt(agent_def, agent_input)

    messages = [
        {"role": "system", "content": system_prompt},
        *agent_input.conversation_history[-settings.SHORT_TERM_MEMORY_WINDOW:],
        {"role": "user", "content": agent_input.task},
    ]

    ctokens = connector_tokens_db or {}
    result_text, tool_results, tokens_used = await _run_with_tools(
        messages, tool_schemas, model_id, api_key, agent_input, ctokens
    )

    return AgentOutput(
        agent_id=agent_input.agent_id,
        agent_name=agent_input.agent_name,
        task_description=agent_input.task,
        task_done=True,
        data={"response": result_text, "tool_results": tool_results},
        resource_usage={"tokens_used": tokens_used, "time_taken_ms": 0},
        metadata={"model_used": model_id},
    )


@retry(
    stop=stop_after_attempt(settings.LLM_MAX_RETRIES),
    wait=wait_exponential_jitter(
        initial=settings.LLM_RETRY_WAIT_MIN,
        max=settings.LLM_RETRY_WAIT_MAX,
        jitter=settings.LLM_RETRY_JITTER,
    ),
    retry=retry_if_exception(_is_retriable),
    before_sleep=before_sleep_log(logger, 30),
    reraise=True,
)
async def _run_with_tools(
    messages: list[dict],
    tool_schemas: list[dict],
    model_id: str,
    api_key: str,
    agent_input: AgentInput,
    connector_tokens_db: dict[str, dict],
) -> tuple[str, list[dict], int]:
    kwargs: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "api_key": api_key,
        "metadata": {"trace_name": f"dynamic_agent_{agent_input.agent_name}"},
    }
    if tool_schemas:
        kwargs["tools"] = tool_schemas
        kwargs["tool_choice"] = "auto"

    response = await litellm.acompletion(**kwargs)
    msg = response.choices[0].message
    tokens_used: int = getattr(getattr(response, "usage", None), "total_tokens", 0) or 0

    tool_results: list[dict] = []
    if getattr(msg, "tool_calls", None):
        tool_results = await _execute_tool_calls(msg.tool_calls, connector_tokens_db)

    return msg.content or "", tool_results, tokens_used


async def _execute_tool_calls(
    tool_calls: list,
    connector_tokens_db: dict[str, dict],
) -> list[dict]:
    registry = get_registry()
    results = []
    for tc in tool_calls:
        fn_name = tc.function.name
        fn = registry.get_callable(fn_name)
        if not fn:
            results.append({"tool": fn_name, "error": "Tool not found"})
            continue
        try:
            args = json.loads(tc.function.arguments)
            connector_slug = getattr(fn, "connector", "")
            if connector_slug and connector_slug in connector_tokens_db:
                tokens = connector_tokens_db[connector_slug]
                args["access_token"] = tokens.get("access_token", "")
                if "instance_url" in tokens:
                    args["instance_url"] = tokens["instance_url"]
            result = await fn(**args)
            results.append({"tool": fn_name, "result": result})
        except (TypeError, json.JSONDecodeError) as e:
            logger.error("Tool %s argument error: %s", fn_name, e)
            results.append({"tool": fn_name, "error": str(e)})
        except Exception as e:
            logger.exception("Tool %s execution failed", fn_name)
            results.append({"tool": fn_name, "error": str(e)})
    return results
