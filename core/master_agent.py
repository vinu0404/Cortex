import json
import logging

import litellm
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.common.exceptions import CircularDependencyError, PlanValidationError
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
) -> tuple[ExecutionPlan, str]:
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
    return ExecutionPlan.model_validate_json(raw), raw


async def generate_plan(
    query: str,
    agents_db: dict[str, dict],
    conversation_history: list[dict],
    long_term_memory: LongTermMemory,
    model_id: str,
    api_key: str,
    conversation_id: str,
    is_embed: bool = False,
    timezone: str = "UTC",
) -> ExecutionPlan:
    registry = get_registry()

    def _agent_context_line(name: str, info: dict, tool_names: list[str]) -> str:
        lines = [f"- {name}: {info.get('system_prompt', '')[:120]}"]
        for kb in info.get("kb_info", []):
            desc = f" — {kb['description']}" if kb.get("description") else ""
            lines.append(f"  [KB: {kb['name']}{desc}]")
        for wc in info.get("collection_info", []):
            desc = f" — {wc['description']}" if wc.get("description") else ""
            lines.append(f"  [WebCollection: {wc['name']}{desc}]")
        if tool_names:
            lines.append(f"  [Tools: {', '.join(tool_names)}]")
        return "\n".join(lines)

    agent_tool_names: dict[str, list[str]] = {}
    for name, info in agents_db.items():
        names = [t["tool"] for t in info.get("tools_config", [])]
        if info.get("kb_ids"):
            names.append("knowledge_base_search")
        if info.get("collection_ids"):
            names.append("collection_search")
        agent_tool_names[name] = names

    agents_json = "\n".join(
        _agent_context_line(name, info, agent_tool_names.get(name, []))
        for name, info in agents_db.items()
    )

    tools_by_agent = {}
    for name, names in agent_tool_names.items():
        if names:
            schemas = registry.get_tool_schemas(names)
            tools_by_agent[name] = schemas

    prompt_text = get_compiled_prompt("master_agent", {
        "agents_json": agents_json or "No custom agents configured.",
        "conversation_history": str(conversation_history[-settings.SHORT_TERM_MEMORY_WINDOW:]),
        "long_term_memory": json.dumps(long_term_memory.model_dump()),
        "query": query,
    }, timezone=timezone)

    if is_embed:
        prompt_text += (
            "\n\n## Request Source: Embedded Chatbot\n"
            "This query comes from an embedded chatbot on an external website — not the workspace owner. "
            "Prefer agents with Knowledge Bases [KB] or Website Collections [WC] attached. "
            "Avoid routing to agents whose only capability is external API tools (email, calendar, CRM) "
            "unless the user explicitly asks for such actions. "
            "Prioritise comprehensive, self-contained answers."
        )

    messages = [{"role": "user", "content": prompt_text}]

    last_error: str | None = None
    last_raw: str | None = None
    for attempt in range(3):
        if attempt > 0 and last_raw and last_error:
            messages.append({"role": "assistant", "content": last_raw})
            messages.append({
                "role": "user",
                "content": (
                    f"Your plan failed validation: {last_error}\n"
                    "Fix the issue and return a corrected plan as JSON. "
                    "Only use agent names from the list provided. "
                    "Ensure all depends_on values reference agent_ids defined in the same plan. "
                    "Each step must have a unique agent_id. "
                    "Do not create circular dependencies."
                ),
            })
        try:
            plan, last_raw = await _call_master_llm(messages, model_id, api_key, conversation_id)
        except Exception as e:
            raise PlanValidationError(f"Master agent failed to generate plan: {e}") from e
        try:
            _validate_plan(plan, agents_db)
            return plan
        except PlanValidationError as e:
            last_error = str(e)
            logger.warning("Plan validation failed (attempt %d/3): %s", attempt + 1, last_error)

    raise PlanValidationError(f"Plan validation failed after 3 attempts. Last error: {last_error}")


def _validate_plan(plan: ExecutionPlan, agents_db: dict[str, dict]) -> None:
    from core.dependency_resolver import resolve_stages
    from core.schemas import ResolvedAgentTask

    known = set(agents_db.keys())
    for step in plan.steps:
        if step.agent_name not in known:
            raise PlanValidationError(
                f"Unknown agent '{step.agent_name}'. Available agents: {sorted(known)}"
            )
    if len({step.agent_id for step in plan.steps}) != len(plan.steps):
        raise PlanValidationError("Duplicate agent_id in plan — each step must have a unique agent_id")
    ids = {step.agent_id for step in plan.steps}
    for step in plan.steps:
        for dep in step.depends_on:
            if dep not in ids:
                raise PlanValidationError(
                    f"Step '{step.agent_id}' depends on unknown runtime_id '{dep}'"
                )
    try:
        resolve_stages([
            ResolvedAgentTask(
                agent_id=s.agent_id, agent_name=s.agent_name,
                task=s.task, depends_on=s.depends_on, tools=s.tools,
            )
            for s in plan.steps
        ])
    except CircularDependencyError:
        raise PlanValidationError("Circular dependency detected — agents cannot depend on each other in a cycle")
