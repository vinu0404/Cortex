import json
import logging

import litellm
from pydantic import BaseModel, Field
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.common.langfuse_client import get_compiled_prompt
from app.common.token_utils import TokenUsage, calculate_usage
from config.settings import get_settings
from core.schemas import AgentOutput, LongTermMemory, SuggestionsOutput

settings = get_settings()
logger = logging.getLogger(__name__)


class ComposerArtifact(BaseModel):
    type: str  # "mermaid" | "pdf" | "csv" | "code"
    title: str
    content: str
    language: str | None = None  # for code artifacts
    filename: str | None = None  # for pdf/csv downloads
    url: str | None = None  # B2 presigned URL after upload (pdf/csv)


class ComposerOutput(BaseModel):
    response: str
    artifacts: list[ComposerArtifact] = Field(default_factory=list)


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
async def _call_composer_llm(
    prompt_text: str,
    model_id: str,
    api_key: str,
    conversation_id: str,
    system_prompt: str | None = None,
) -> tuple[ComposerOutput, TokenUsage]:
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt_text})
    response = await litellm.acompletion(
        model=model_id,
        messages=messages,
        response_format={"type": "json_object"},
        api_key=api_key,
        metadata={
            "trace_name": "composer_agent",
            "trace_session_id": conversation_id,
            "tags": ["orchestration"],
        },
    )
    usage = calculate_usage(response, model_id)
    return ComposerOutput.model_validate_json(response.choices[0].message.content), usage


async def compose_response(
    query: str,
    agent_outputs: dict[str, AgentOutput],
    conversation_history: list[dict],
    long_term_memory: LongTermMemory,
    model_id: str,
    api_key: str,
    conversation_id: str,
    persona: str | None = None,
    timezone: str = "UTC",
) -> tuple[str, list[ComposerArtifact], list[str], TokenUsage]:
    """Returns (response_text, artifacts, suggested_questions, token_usage)."""
    successful = {k: v for k, v in agent_outputs.items() if v.task_done and not v.error}
    failed = {k: v for k, v in agent_outputs.items() if v.error}

    outputs_summary = "\n\n".join(
        f"### {o.agent_name}\n{(o.data.get('response', '') if o.data else '')[:settings.COMPOSER_AGENT_OUTPUT_MAX_CHARS]}"
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
    }, timezone=timezone)

    ltm_system: str | None = None
    if long_term_memory.critical_facts:
        facts = {k: v for k, v in long_term_memory.critical_facts.items() if v not in (None, "", [], {})}
        if facts:
            ltm_system = (
                "## User Context\n"
                "You know these facts about the user. Use them when answering questions about the user — "
                "do NOT say you don't know if the answer is present here.\n"
                + json.dumps(facts)
            )

    result, tokens = await _call_composer_llm(prompt_text, model_id, api_key, conversation_id, system_prompt=ltm_system)

    artifacts = []
    for artifact in result.artifacts:
        if artifact.type == "pdf":
            artifact = await _generate_pdf(artifact)
        artifacts.append(artifact)

    suggestions: list[str] = []
    if settings.ENABLE_SUGGESTIONS:
        suggestions = await _generate_suggestions(
            conversation_history, result.response, model_id, api_key, conversation_id
        )

    return result.response, artifacts, suggestions, tokens


async def _generate_pdf(artifact: ComposerArtifact) -> ComposerArtifact:
    """Generate PDF bytes from text content, return as base64."""
    try:
        import base64
        import io
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=letter)
        styles = getSampleStyleSheet()
        story = []
        for line in artifact.content.split("\n"):
            if line.strip():
                story.append(Paragraph(line, styles["Normal"]))
                story.append(Spacer(1, 6))
        doc.build(story)
        pdf_b64 = base64.b64encode(buf.getvalue()).decode()
        return ComposerArtifact(
            type="pdf",
            title=artifact.title,
            content=pdf_b64,
            filename=artifact.filename or f"{artifact.title.lower().replace(' ', '_')}.pdf",
        )
    except ImportError:
        logger.error("reportlab not installed — returning PDF as text")
        return artifact


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
            response_format={"type": "json_object"},
            api_key=api_key,
            metadata={"trace_name": "suggestion_generation", "trace_session_id": conversation_id},
        )
        result = SuggestionsOutput.model_validate_json(resp.choices[0].message.content)
        return result.questions
    except Exception as e:
        logger.error("Suggestion generation failed: %s", e)
        return []
