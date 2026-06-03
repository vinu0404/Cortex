import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from config.settings import get_settings
from core.dependency_resolver import get_affected_tasks, resolve_stages
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


@dataclass
class PlanCallbacks:
    on_hitl_needed: Callable
    on_agent_done: Callable | None = None
    on_agent_start: Callable | None = None


@dataclass
class RetryInput:
    prior_state: dict[str, AgentOutput]
    retry_from_id: str


class OrchestrationContext:
    def __init__(
        self,
        user_id: UUID,
        conversation_id: UUID,
        workspace_id: UUID,
        conversation_history: list[dict],
        long_term_memory: LongTermMemory,
        agents_db: dict[str, dict],
        api_keys_db: dict[UUID, str],
        connector_tokens_db: dict[str, dict],
        persona: str | None = None,
        is_embed: bool = False,
        timezone: str = "UTC",
        retry_context: str | None = None,
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
        self.is_embed = is_embed
        self.timezone = timezone
        self.retry_context = retry_context


async def execute_plan(
    plan: ExecutionPlan,
    context: OrchestrationContext,
    callbacks: PlanCallbacks,
) -> dict[str, AgentOutput]:
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
    await _execute_stages(stages, shared_state, context, callbacks)
    failed_ids = {aid for aid, out in shared_state.items() if not out.task_done}
    if failed_ids:
        await _retry_failed(tasks, failed_ids, shared_state, context, callbacks)
    return shared_state


async def execute_plan_partial(
    plan: ExecutionPlan,
    retry_input: RetryInput,
    context: OrchestrationContext,
    callbacks: PlanCallbacks,
) -> dict[str, AgentOutput]:
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
    affected = get_affected_tasks({retry_input.retry_from_id}, tasks)
    shared_state = dict(retry_input.prior_state)
    retry_ctx = f"User-triggered retry of agent {retry_input.retry_from_id}"
    new_ctx = _with_retry_context(context, retry_ctx)
    await _execute_stages(resolve_stages(affected), shared_state, new_ctx, callbacks)
    return shared_state


async def _retry_failed(
    tasks: list[ResolvedAgentTask],
    failed_ids: set[str],
    shared_state: dict[str, AgentOutput],
    context: OrchestrationContext,
    callbacks: PlanCallbacks,
) -> None:
    affected = get_affected_tasks(failed_ids, tasks)
    retry_ctx = "; ".join(
        (shared_state[aid].error or "failed")[:80] for aid in failed_ids
    )
    new_ctx = _with_retry_context(context, retry_ctx)
    await _execute_stages(resolve_stages(affected), shared_state, new_ctx, callbacks)


async def _execute_stages(
    stages: list[list[ResolvedAgentTask]],
    shared_state: dict[str, AgentOutput],
    context: OrchestrationContext,
    callbacks: PlanCallbacks,
) -> None:
    for stage in stages:
        _fire_start(stage, callbacks)
        results = await asyncio.gather(
            *[_run_agent(task, context, shared_state, callbacks.on_hitl_needed) for task in stage],
            return_exceptions=True,
        )
        await _collect_results(stage, results, shared_state, callbacks)


def _fire_start(stage: list[ResolvedAgentTask], callbacks: PlanCallbacks) -> None:
    if callbacks.on_agent_start:
        for task in stage:
            callbacks.on_agent_start(task.agent_id, task.agent_name)


async def _collect_results(
    stage: list[ResolvedAgentTask],
    results: list,
    shared_state: dict[str, AgentOutput],
    callbacks: PlanCallbacks,
) -> None:
    for task, result in zip(stage, results):
        output = result if not isinstance(result, Exception) else _error_output(task, result)
        shared_state[task.agent_id] = output
        if callbacks.on_agent_done:
            await callbacks.on_agent_done(output)


def _error_output(task: ResolvedAgentTask, exc: Exception) -> AgentOutput:
    return AgentOutput(
        agent_id=task.agent_id,
        agent_name=task.agent_name,
        task_description=task.task,
        task_done=False,
        error=str(exc),
    )


def _with_retry_context(ctx: OrchestrationContext, retry_ctx: str) -> OrchestrationContext:
    """Shallow copy of context with retry_context replaced."""
    new = OrchestrationContext.__new__(OrchestrationContext)
    new.__dict__.update(ctx.__dict__)
    new.retry_context = retry_ctx
    return new


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

    dependency_outputs = _build_dependency_outputs(task, shared_state)
    hitl_context = await _check_hitl(task, agent_def, on_hitl_needed)
    agent_input = _build_agent_input(task, context, dependency_outputs, hitl_context)
    connector_tokens, agent_input = _build_per_agent_tokens(agent_def, agent_input, context)

    api_key_id = agent_def.get("api_key_id")
    raw_key = context.api_keys_db.get(api_key_id, "") if api_key_id else ""
    model_id = agent_def.get("model_id") or settings.DEFAULT_MODEL

    start = time.monotonic()
    output = await run_dynamic_agent(
        agent_input, agent_def, model_id, raw_key, connector_tokens, is_embed=context.is_embed
    )
    elapsed_ms = int((time.monotonic() - start) * 1000)
    output.resource_usage["time_taken_ms"] = elapsed_ms
    _stamp_execution_metadata(output, agent_input, task, context)
    return output


def _build_task_prompt(task: str, retry_context: str | None) -> str:
    if not retry_context:
        return task
    return f"{task}\n\n[RETRY: {retry_context}. Use your assigned tools; do not hallucinate.]"


def _build_dependency_outputs(
    task: ResolvedAgentTask,
    shared_state: dict[str, AgentOutput],
) -> dict:
    dependency_outputs = {}
    for dep_id in task.depends_on:
        if dep_id not in shared_state:
            continue
        dep_output = shared_state[dep_id]
        if not dep_output.task_done:
            err = (dep_output.error or "upstream agent failed")[:100]
            logger.warning("Upstream %s failed; passing error to %s: %s", dep_id, task.agent_id, err)
            dependency_outputs[dep_id] = {
                "error": err,
                "note": "Upstream agent failed. Use your own knowledge and assigned tools to complete your task. Do not hallucinate or make up data.",
            }
        else:
            dependency_outputs[dep_id] = dep_output.data
    return dependency_outputs


def _build_agent_input(
    task: ResolvedAgentTask,
    context: OrchestrationContext,
    dependency_outputs: dict,
    hitl_context: HitlResolvedDecision | None,
) -> AgentInput:
    task_prompt = _build_task_prompt(task.task, context.retry_context)
    return AgentInput(
        agent_id=task.agent_id,
        agent_name=task.agent_name,
        task=task_prompt,
        conversation_history=context.conversation_history,
        long_term_memory=context.long_term_memory,
        dependency_outputs=dependency_outputs,
        tools=task.tools,
        metadata={
            "user_id": str(context.user_id),
            "conversation_id": str(context.conversation_id),
            "workspace_id": str(context.workspace_id),
            "persona": context.persona,
            "timezone": context.timezone,
        },
        hitl_context=hitl_context,
    )


def _build_per_agent_tokens(
    agent_def: dict,
    agent_input: AgentInput,
    context: OrchestrationContext,
) -> tuple[dict, AgentInput]:
    kb_ids = agent_def.get("kb_ids", [])
    collection_ids = agent_def.get("collection_ids", [])
    tokens = dict(context.connector_tokens_db)

    if kb_ids:
        tokens["__kb__"] = {"kb_ids": [str(k) for k in kb_ids], "user_id": str(context.user_id)}
        if "knowledge_base_search" not in agent_input.tools:
            agent_input = agent_input.model_copy(
                update={"tools": list(agent_input.tools) + ["knowledge_base_search"]}
            )
    if collection_ids:
        tokens["__website__"] = {"collection_ids": [str(c) for c in collection_ids], "user_id": str(context.user_id)}
        if "collection_search" not in agent_input.tools:
            agent_input = agent_input.model_copy(
                update={"tools": list(agent_input.tools) + ["collection_search"]}
            )
    return tokens, agent_input


def _stamp_execution_metadata(
    output: AgentOutput,
    agent_input: AgentInput,
    task: ResolvedAgentTask,
    context: OrchestrationContext,
) -> None:
    prior_attempt = output.execution_metadata.get("retry_attempt", 0)
    retry_attempt = prior_attempt + 1 if context.retry_context else prior_attempt
    output.execution_metadata.update({
        "input_task": agent_input.task,
        "dependency_outputs": agent_input.dependency_outputs,
        "tools": list(task.tools),
        "retry_attempt": retry_attempt,
    })


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
