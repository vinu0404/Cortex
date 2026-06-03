import asyncio
import logging

from app.common.retry import aembedding_with_retry
from config.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


async def _embed_batch(texts: list[str], model: str, api_key: str) -> list[list[float]]:
    response = await aembedding_with_retry(model=model, input=texts, api_key=api_key)
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
