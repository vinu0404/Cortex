import logging

import litellm
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.common.langfuse_client import get_compiled_prompt
from config.settings import get_settings
from core.schemas import AgentOutput, LongTermMemory, SuggestionsOutput

settings = get_settings()
logger = logging.getLogger(__name__)


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
    before_sleep=before_sleep_log(logger, 30),
    reraise=True,
)
async def compose_response(
    query: str,
    agent_outputs: dict[str, AgentOutput],
    conversation_history: list[dict],
    long_term_memory: LongTermMemory,
    model_id: str,
    api_key: str,
    conversation_id: str,
    persona: str | None = None,
) -> tuple[str, list[str]]:
    """Returns (response_text, suggested_questions)."""
    successful = {k: v for k, v in agent_outputs.items() if v.task_done and not v.error}
    failed = {k: v for k, v in agent_outputs.items() if v.error}

    outputs_summary = "\n\n".join(
        f"### {o.agent_name}\n{o.data.get('response', '') if o.data else ''}"
        for o in successful.values()
    )
    failed_summary = ", ".join(o.agent_name for o in failed.values())

    prompt_text = get_compiled_prompt("composer_agent", {
        "query": query,
        "agent_outputs": outputs_summary or "No outputs available.",
        "failed_agents": failed_summary or "None",
        "conversation_history": str(conversation_history[-6:]),
        "long_term_memory": str(long_term_memory.model_dump()),
        "persona": persona or "Default — be helpful and concise.",
    })

    response = await litellm.acompletion(
        model=model_id,
        messages=[{"role": "user", "content": prompt_text}],
        api_key=api_key,
        stream=False,
        metadata={
            "trace_name": "composer_agent",
            "trace_session_id": conversation_id,
            "tags": ["orchestration"],
        },
    )
    response_text = response.choices[0].message.content or ""

    suggestions: list[str] = []
    if settings.ENABLE_SUGGESTIONS:
        suggestions = await _generate_suggestions(
            conversation_history, response_text, model_id, api_key, conversation_id
        )

    return response_text, suggestions


async def _generate_suggestions(
    conversation_history: list[dict],
    last_response: str,
    model_id: str,
    api_key: str,
    conversation_id: str,
) -> list[str]:
    try:
        summary = " | ".join(m.get("content", "")[:80] for m in conversation_history[-4:])
        prompt_text = get_compiled_prompt("suggestion_generation", {
            "conversation_summary": summary,
            "last_response": last_response[:400],
        })
        resp = await litellm.acompletion(
            model=model_id,
            messages=[{"role": "user", "content": prompt_text}],
            response_format=SuggestionsOutput,
            api_key=api_key,
            metadata={"trace_name": "suggestion_generation", "trace_session_id": conversation_id},
        )
        result = SuggestionsOutput.model_validate_json(resp.choices[0].message.content)
        return result.questions
    except Exception as e:
        logger.warning("Suggestion generation failed: %s", e)
        return []
