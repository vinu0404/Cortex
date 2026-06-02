import logging

import litellm
from pydantic import BaseModel

from app.common.langfuse_client import get_compiled_prompt
from config.settings import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

BLOCKED_INPUT_MESSAGE = (
    "Your message was blocked by content guardrails. "
    "Please ensure your request doesn't attempt to override AI instructions or contain inappropriate content."
)
SAFE_RESPONSE_FALLBACK = (
    "I'm unable to provide that response as it doesn't meet content safety guidelines. "
    "Please ask something else."
)


class GuardrailOutput(BaseModel):
    blocked: bool
    category: str = "safe"
    reason: str = ""


async def check_input(query: str, api_key: str | None) -> GuardrailOutput:
    return await _classify(query, "guardrail_input", api_key, "input")


async def check_output(text: str, api_key: str | None) -> GuardrailOutput:
    return await _classify(text, "guardrail_output", api_key, "output")


async def _classify(content: str, prompt_name: str, api_key: str | None, label: str) -> GuardrailOutput:
    """
    Fail-open: any error (API failure, JSON parse, Pydantic validation)
    returns GuardrailOutput(blocked=False) so guardrail never breaks the pipeline.
    """
    try:
        system = get_compiled_prompt(prompt_name, {})
        resp = await litellm.acompletion(
            model=settings.GUARDRAILS_MODEL,
            api_key=api_key,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": content[:4000]},
            ],
            response_format={"type": "json_object"},
            max_tokens=100,
            temperature=0,
            metadata={"trace_name": f"guardrail_{label}"},
        )
        raw = resp.choices[0].message.content or "{}"
        return GuardrailOutput.model_validate_json(raw)
    except Exception as exc:
        logger.warning("Guardrail %s failed (%s) — letting through", label, exc)
        return GuardrailOutput(blocked=False)
