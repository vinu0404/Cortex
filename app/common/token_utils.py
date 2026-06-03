import logging
from dataclasses import dataclass, field
from typing import Any

import litellm

logger = logging.getLogger(__name__)


@dataclass
class TokenUsage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    agent_breakdown: dict[str, "TokenUsage"] = field(default_factory=dict)


def calculate_usage(response: Any, model: str) -> TokenUsage:
    usage = getattr(response, "usage", None)
    if not usage:
        return TokenUsage(model=model)

    input_t = getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0)
    output_t = getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0)
    total_t = input_t + output_t

    cost = _calculate_cost(model, input_t, output_t)
    return TokenUsage(
        model=model,
        input_tokens=input_t,
        output_tokens=output_t,
        total_tokens=total_t,
        cost_usd=cost,
    )


def _with_provider_prefix(model: str) -> str:
    base = model.split("/")[-1]
    if base.startswith("claude-"):
        return f"anthropic/{base}"
    if base.startswith("gemini-"):
        return f"gemini/{base}"
    if base.startswith(("mistral-", "mixtral-")):
        return f"mistral/{base}"
    if base.startswith(("llama-", "llama3")):
        return f"groq/{base}"
    if base.startswith("deepseek-"):
        return f"deepseek/{base}"
    return model  # OpenAI and already-prefixed models need no change


def _calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    # Try with provider prefix first (e.g. anthropic/claude-3-5-sonnet-20241022),
    # then bare name — litellm needs the prefix for non-OpenAI models.
    for model_name in dict.fromkeys([_with_provider_prefix(model), model]):
        try:
            prompt_cost, completion_cost = litellm.cost_per_token(
                model=model_name,
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
            )
            return prompt_cost + completion_cost
        except Exception as exc:
            logger.debug("litellm.cost_per_token failed for model alias %r: %s", model_name, exc)
    logger.warning("litellm.cost_per_token failed for model %r — cost recorded as 0.0", model)
    return 0.0
