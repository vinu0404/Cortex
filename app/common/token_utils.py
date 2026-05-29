from dataclasses import dataclass, field

# Cost per 1M tokens (input, output) in USD
MODEL_PRICING: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-opus-4-7": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (0.8, 4.0),
    # OpenAI
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4-turbo": (10.0, 30.0),
    "o1": (15.0, 60.0),
    "o3-mini": (1.1, 4.4),
    # Google
    "gemini-2.0-flash": (0.1, 0.4),
    "gemini-1.5-pro": (1.25, 5.0),
    # Groq
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "mixtral-8x7b-32768": (0.24, 0.24),
    # Mistral
    "mistral-large-latest": (3.0, 9.0),
    "mistral-small-latest": (0.2, 0.6),
}


@dataclass
class TokenUsage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    agent_breakdown: dict[str, "TokenUsage"] = field(default_factory=dict)


def calculate_usage(response: object, model: str) -> TokenUsage:
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


def _calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    base = model.split("/")[-1]
    pricing = MODEL_PRICING.get(base) or MODEL_PRICING.get(model)
    if not pricing:
        return 0.0
    input_cost, output_cost = pricing
    return (input_tokens * input_cost + output_tokens * output_cost) / 1_000_000
