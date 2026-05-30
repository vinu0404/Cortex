import logging

import litellm
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.common.exceptions import PlanValidationError
from app.common.langfuse_client import get_compiled_prompt
from config.settings import get_settings
from core.schemas import ExecutionPlan, LongTermMemory
from tools.registry import get_registry

settings = get_settings()
logger = logging.getLogger(__name__)

_RETRIABLE = (Exception,)


def _is_retriable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "rate" in msg or "timeout" in msg or "connection" in msg or "500" in msg or "503" in msg


@retry(
    stop=stop_after_attempt(settings.LLM_MAX_RETRIES),
    wait=wait_exponential_jitter(
        initial=settings.LLM_RETRY_WAIT_MIN,
        max=settings.LLM_RETRY_WAIT_MAX,
        jitter=settings.LLM_RETRY_JITTER,
    ),
    retry=retry_if_exception(_is_retriable),
    before_sleep=before_sleep_log(logger, 30),  # WARNING
    reraise=True,
)
async def _call_master_llm(
    messages: list[dict],
    model_id: str,
    api_key: str,
    conversation_id: str,
) -> ExecutionPlan:
    response = await litellm.acompletion(
        model=model_id,
        messages=messages,
        response_format={"type": "json_object"},
        api_key=api_key,
        metadata={
            "trace_name": "master_agent",
            "trace_session_id": conversation_id,
            "tags": ["orchestration"],
        },
    )
    raw = response.choices[0].message.content
    return ExecutionPlan.model_validate_json(raw)


async def generate_plan(
    query: str,
    agents_db: dict[str, dict],
    conversation_history: list[dict],
    long_term_memory: LongTermMemory,
    model_id: str,
    api_key: str,
    conversation_id: str,
) -> ExecutionPlan:
    registry = get_registry()

    def _agent_context_line(name: str, info: dict) -> str:
        lines = [f"- {name}: {info.get('system_prompt', '')[:120]}"]
        for kb in info.get("kb_info", []):
            desc = f" — {kb['description']}" if kb.get("description") else ""
            lines.append(f"  [KB: {kb['name']}{desc}]")
        for wc in info.get("collection_info", []):
            desc = f" — {wc['description']}" if wc.get("description") else ""
            lines.append(f"  [WebCollection: {wc['name']}{desc}]")
        return "\n".join(lines)

    agents_json = "\n".join(_agent_context_line(name, info) for name, info in agents_db.items())

    tools_by_agent = {}
    for name, info in agents_db.items():
        tool_names = [t["tool"] for t in info.get("tools_config", [])]
        if info.get("kb_ids"):
            tool_names.append("knowledge_base_search")
        if info.get("collection_ids"):
            tool_names.append("collection_search")
        if tool_names:
            schemas = registry.get_tool_schemas(tool_names)
            tools_by_agent[name] = schemas

    tools_json = "\n".join(
        f"  {agent}: " + ", ".join(t["name"] for t in tools)
        for agent, tools in tools_by_agent.items()
    )

    prompt_text = get_compiled_prompt("master_agent", {
        "agents_json": agents_json or "No custom agents configured.",
        "tools_json": tools_json or "No tools configured.",
        "conversation_history": str(conversation_history[-6:]),
        "long_term_memory": str(long_term_memory.model_dump()),
        "query": query,
    })

    messages = [{"role": "user", "content": prompt_text}]

    try:
        plan = await _call_master_llm(messages, model_id, api_key, conversation_id)
    except Exception as e:
        raise PlanValidationError(f"Master agent failed to generate plan: {e}") from e

    _validate_plan(plan, agents_db)
    return plan


def _validate_plan(plan: ExecutionPlan, agents_db: dict[str, dict]) -> None:
    known = set(agents_db.keys())
    for step in plan.steps:
        if step.agent_name not in known:
            raise PlanValidationError(
                f"Unknown agent '{step.agent_name}' in plan. Known: {sorted(known)}"
            )
    ids = {step.agent_id for step in plan.steps}
    for step in plan.steps:
        for dep in step.depends_on:
            if dep not in ids:
                raise PlanValidationError(
                    f"Step '{step.agent_id}' depends on unknown runtime_id '{dep}'"
                )
