from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

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


def get_compiled_prompt(name: str, variables: dict | None = None, timezone: str = "UTC") -> str:
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = ZoneInfo("UTC")
    current_time = datetime.now(tz).strftime("%A, %B %-d %Y, %I:%M %p %Z")
    vars_to_use = {"current_time": current_time, **(variables or {})}
    lf = get_langfuse()
    prompt = lf.get_prompt(
        name,
        label="production",
        cache_ttl_seconds=settings.LANGFUSE_PROMPT_CACHE_TTL,
    )
    return prompt.compile(**vars_to_use)
