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
from core.schemas import AgentInput, AgentOutput, ArtifactPreview
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


def _build_tool_schemas(tool_names: list[str]) -> list[dict]:
    registry = get_registry()
    schemas = []
    for name in tool_names:
        fn = registry.get_callable(name)
        if not fn:
            continue
        import inspect
        sig = inspect.signature(fn)
        props = {}
        required = []
        for param_name, param in sig.parameters.items():
            if param_name in ("access_token", "instance_url"):
                continue
            if param.default is inspect.Parameter.empty:
                required.append(param_name)
            props[param_name] = {"type": "string", "description": f"{param_name} parameter"}
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

    result_text, tool_results = await _run_with_tools(messages, tool_schemas, model_id, api_key, agent_input)

    return AgentOutput(
        agent_id=agent_input.agent_id,
        agent_name=agent_input.agent_name,
        task_description=agent_input.task,
        task_done=True,
        data={"response": result_text, "tool_results": tool_results},
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
) -> tuple[str, list[dict]]:
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

    tool_results: list[dict] = []
    if getattr(msg, "tool_calls", None):
        tool_results = await _execute_tool_calls(msg.tool_calls, agent_input)

    return msg.content or "", tool_results


async def _execute_tool_calls(tool_calls: list, agent_input: AgentInput) -> list[dict]:
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
            result = await fn(**args)
            results.append({"tool": fn_name, "result": result})
        except Exception as e:
            logger.warning("Tool %s failed: %s", fn_name, e)
            results.append({"tool": fn_name, "error": str(e)})
    return results
