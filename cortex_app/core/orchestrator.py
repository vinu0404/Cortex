import asyncio
import logging
import time
from collections.abc import Callable
from uuid import UUID

from config.settings import get_settings
from core.dependency_resolver import resolve_stages
from core.schemas import (
    AgentInput,
    AgentOutput,
    ExecutionPlan,
    HitlResolvedDecision,
    LongTermMemory,
    ResolvedAgentTask,
)
from tools.registry import get_registry

settings = get_settings()

logger = logging.getLogger(__name__)


class OrchestrationContext:
    def __init__(
        self,
        user_id: UUID,
        conversation_id: UUID,
        workspace_id: UUID,
        conversation_history: list[dict],
        long_term_memory: LongTermMemory,
        agents_db: dict[str, dict],  # name → {id, system_prompt, model_id, api_key_id, tools_config}
        api_keys_db: dict[UUID, str],  # api_key_id → decrypted key
        connector_tokens_db: dict[str, dict],  # connector_slug → decrypted token dict
        persona: str | None = None,
    ):
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.workspace_id = workspace_id
        self.conversation_history = conversation_history
        self.long_term_memory = long_term_memory
        self.agents_db = agents_db
        self.api_keys_db = api_keys_db
        self.connector_tokens_db = connector_tokens_db
        self.persona = persona


async def execute_plan(
    plan: ExecutionPlan,
    context: OrchestrationContext,
    on_hitl_needed: Callable[[str, str, list[str]], asyncio.Future],
    on_agent_done: Callable[[AgentOutput], None] | None = None,
) -> dict[str, AgentOutput]:
    """
    Execute plan stages in order. Within each stage, tasks run in parallel.
    Returns shared_state: runtime_agent_id → AgentOutput.
    """
    tasks = [
        ResolvedAgentTask(
            agent_id=step.agent_id,
            agent_name=step.agent_name,
            task=step.task,
            depends_on=step.depends_on,
            tools=step.tools,
        )
        for step in plan.steps
    ]

    stages = resolve_stages(tasks)
    shared_state: dict[str, AgentOutput] = {}

    for stage in stages:
        results = await asyncio.gather(
            *[_run_agent(task, context, shared_state, on_hitl_needed) for task in stage],
            return_exceptions=True,
        )
        for task, result in zip(stage, results):
            if isinstance(result, Exception):
                output = AgentOutput(
                    agent_id=task.agent_id,
                    agent_name=task.agent_name,
                    task_description=task.task,
                    task_done=False,
                    error=str(result),
                )
            else:
                output = result
            shared_state[task.agent_id] = output
            if on_agent_done:
                on_agent_done(output)

    return shared_state


async def _run_agent(
    task: ResolvedAgentTask,
    context: OrchestrationContext,
    shared_state: dict[str, AgentOutput],
    on_hitl_needed: Callable,
) -> AgentOutput:
    from core.dynamic_agent import run_dynamic_agent

    agent_def = context.agents_db.get(task.agent_name)
    if not agent_def:
        return AgentOutput(
            agent_id=task.agent_id,
            agent_name=task.agent_name,
            task_description=task.task,
            task_done=False,
            error=f"Agent '{task.agent_name}' not found in workspace",
        )

    dependency_outputs = {}
    for dep_id in task.depends_on:
        if dep_id in shared_state:
            dep_data = shared_state[dep_id].data
            if dep_data is None:
                logger.warning("Upstream agent %s produced no data; task %s may be incomplete", dep_id, task.agent_id)
            dependency_outputs[dep_id] = dep_data

    hitl_context = await _check_hitl(task, agent_def, on_hitl_needed)

    agent_input = AgentInput(
        agent_id=task.agent_id,
        agent_name=task.agent_name,
        task=task.task,
        conversation_history=context.conversation_history,
        long_term_memory=context.long_term_memory,
        dependency_outputs=dependency_outputs,
        tools=task.tools,
        metadata={
            "user_id": str(context.user_id),
            "conversation_id": str(context.conversation_id),
            "workspace_id": str(context.workspace_id),
            "persona": context.persona,
        },
        hitl_context=hitl_context,
    )

    api_key_id = agent_def.get("api_key_id")
    raw_key = context.api_keys_db.get(api_key_id, "") if api_key_id else ""
    model_id = agent_def.get("model_id") or settings.DEFAULT_MODEL

    # Build per-agent connector_tokens with __kb__ injection if agent has KBs
    kb_ids = agent_def.get("kb_ids", [])
    per_agent_tokens = dict(context.connector_tokens_db)
    if kb_ids:
        per_agent_tokens["__kb__"] = {
            "kb_ids": [str(k) for k in kb_ids],
            "user_id": str(context.user_id),
        }
        if "knowledge_base_search" not in agent_input.tools:
            agent_input = agent_input.model_copy(
                update={"tools": list(agent_input.tools) + ["knowledge_base_search"]}
            )

    start = time.monotonic()
    output = await run_dynamic_agent(
        agent_input, agent_def, model_id, raw_key, per_agent_tokens
    )
    elapsed_ms = int((time.monotonic() - start) * 1000)
    output.resource_usage["time_taken_ms"] = elapsed_ms

    return output


async def _check_hitl(
    task: ResolvedAgentTask,
    agent_def: dict,
    on_hitl_needed: Callable,
) -> HitlResolvedDecision | None:
    registry = get_registry()
    tools_config = agent_def.get("tools_config", [])
    tool_names = [t["tool"] for t in tools_config if t["tool"] in task.tools]
    hitl_tools = registry.get_hitl_tools(tool_names)

    if not hitl_tools:
        return None

    future = on_hitl_needed(task.agent_id, task.agent_name, hitl_tools)
    decision: dict = await future
    return HitlResolvedDecision(
        request_id=decision.get("request_id", ""),
        approved=decision.get("approved", False),
        instructions=decision.get("instructions"),
    )
