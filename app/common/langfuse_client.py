from functools import lru_cache

from langfuse import Langfuse

from config.settings import get_settings

settings = get_settings()


@lru_cache(maxsize=1)
def get_langfuse() -> Langfuse:
    return Langfuse(
        public_key=settings.LANGFUSE_PUBLIC_KEY,
        secret_key=settings.LANGFUSE_SECRET_KEY,
        host=settings.LANGFUSE_BASE_URL,
    )


def get_compiled_prompt(name: str, variables: dict | None = None) -> str:
    lf = get_langfuse()
    prompt = lf.get_prompt(
        name,
        label="production",
        cache_ttl_seconds=settings.LANGFUSE_PROMPT_CACHE_TTL,
    )
    return prompt.compile(**(variables or {}))
