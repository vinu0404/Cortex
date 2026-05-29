import asyncio
import logging

import litellm
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from config.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


@retry(
    stop=stop_after_attempt(settings.LLM_MAX_RETRIES),
    wait=wait_exponential_jitter(initial=1, max=30, jitter=2),
    retry=retry_if_exception_type((litellm.RateLimitError, litellm.APIConnectionError, litellm.Timeout)),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
async def _embed_batch(texts: list[str], model: str, api_key: str) -> list[list[float]]:
    response = await litellm.aembedding(model=model, input=texts, api_key=api_key)
    return [item["embedding"] for item in response.data]


async def embed_texts(texts: list[str], api_key: str) -> list[list[float]]:
    """Embed texts in batches of KB_EMBED_BATCH_SIZE. Returns in same order as input."""
    model = settings.KB_EMBEDDING_MODEL
    batch_size = settings.KB_EMBED_BATCH_SIZE
    all_embeddings: list[list[float]] = []

    for i in range(0, len(texts), batch_size):
        batch = texts[i: i + batch_size]
        embeddings = await _embed_batch(batch, model, api_key)
        all_embeddings.extend(embeddings)
        if i + batch_size < len(texts):
            await asyncio.sleep(0.1)

    return all_embeddings
