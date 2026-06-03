import logging
import uuid

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchText,
    MatchValue,
    PointStruct,
    TextIndexParams,
    TokenizerType,
    VectorParams,
)

from config.settings import get_settings
from app.common.retry import async_qdrant_call
from document_pipeline.chunker import Chunk

settings = get_settings()
logger = logging.getLogger(__name__)


def _client() -> AsyncQdrantClient:
    return AsyncQdrantClient(url=settings.QDRANT_URL)


def _collection_name(kb_id: str) -> str:
    return f"kb_{kb_id}"


async def ensure_collection(kb_id: str, redis_client=None) -> None:
    name = _collection_name(kb_id)

    async def _create_if_missing() -> None:
        client = _client()
        try:
            collections = await async_qdrant_call(client.get_collections)
            if name not in {c.name for c in collections.collections}:
                await async_qdrant_call(
                    client.create_collection,
                    collection_name=name,
                    vectors_config=VectorParams(size=settings.KB_EMBEDDING_DIMS, distance=Distance.COSINE),
                )
                logger.info("Created Qdrant collection: %s", name)
        finally:
            await client.close()

    if redis_client is not None:
        async with redis_client.lock(f"qdrant:collection_init:{name}", timeout=15, blocking_timeout=30):
            await _create_if_missing()
    else:
        await _create_if_missing()


async def create_text_index(kb_id: str) -> None:
    """Create full-text payload index on 'text' field. Idempotent."""
    client = _client()
    try:
        await async_qdrant_call(
            client.create_payload_index,
            collection_name=_collection_name(kb_id),
            field_name="text",
            field_schema=TextIndexParams(type="text", tokenizer=TokenizerType.WORD),
        )
    except Exception as e:
        # Only swallow "already exists" (status 400) — re-raise real errors
        msg = str(e).lower()
        if "already exists" in msg or "400" in msg:
            logger.debug("Text index already exists for kb %s", kb_id)
        else:
            logger.error("Failed to create text index for kb %s: %s", kb_id, e)
            raise
    finally:
        await client.close()


async def upsert_chunks(
    kb_id: str,
    doc_id: str,
    filename: str,
    chunks: list[Chunk],
    embeddings: list[list[float]],
) -> None:
    client = _client()
    try:
        points = [
            PointStruct(
                id=str(uuid.uuid4()),
                vector=embeddings[i],
                payload={
                    "doc_id": doc_id,
                    "kb_id": kb_id,
                    "chunk_index": chunk.chunk_index,
                    "filename": filename,
                    "text": chunk.text,
                    "page_start": chunk.page_start,
                    "page_end": chunk.page_end,
                    "section": chunk.section,
                    "chunk_type": chunk.chunk_type,
                },
            )
            for i, chunk in enumerate(chunks)
        ]
        await async_qdrant_call(client.upsert, collection_name=_collection_name(kb_id), points=points)
        logger.info("Upserted %d points to Qdrant collection %s", len(points), _collection_name(kb_id))
    finally:
        await client.close()


async def delete_document_chunks(kb_id: str, doc_id: str) -> None:
    client = _client()
    try:
        await async_qdrant_call(
            client.delete,
            collection_name=_collection_name(kb_id),
            points_selector=Filter(must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]),
        )
    except Exception as e:
        if getattr(e, "status_code", None) == 404:
            logger.warning("Qdrant collection not found when deleting chunks for doc %s (kb %s) — skipping", doc_id, kb_id)
        else:
            logger.error("Failed to delete chunks for doc %s from kb %s", doc_id, kb_id, exc_info=True)
    finally:
        await client.close()


async def delete_collection(kb_id: str) -> None:
    client = _client()
    try:
        await async_qdrant_call(client.delete_collection, _collection_name(kb_id))
    except Exception:
        logger.error("Failed to delete Qdrant collection for kb %s", kb_id, exc_info=True)
    finally:
        await client.close()


async def dense_search(kb_id: str, query_embedding: list[float], top_k: int) -> list[dict]:
    client = _client()
    try:
        results = await async_qdrant_call(
            client.search,
            collection_name=_collection_name(kb_id),
            query_vector=query_embedding,
            limit=top_k,
            with_payload=True,
        )
        return [{"id": str(r.id), "score": r.score, "payload": r.payload} for r in results]
    except Exception:
        logger.error("Dense search failed for kb %s", kb_id, exc_info=True)
        raise
    finally:
        await client.close()


async def text_search(kb_id: str, query: str, top_k: int) -> list[dict]:
    """Keyword search via Qdrant payload text index."""
    client = _client()
    try:
        results, _ = await async_qdrant_call(
            client.scroll,
            collection_name=_collection_name(kb_id),
            scroll_filter=Filter(must=[FieldCondition(key="text", match=MatchText(text=query))]),
            limit=top_k,
            with_payload=True,
        )
        return [{"id": str(r.id), "score": 1.0, "payload": r.payload} for r in results]
    except Exception:
        logger.error("Text search failed for kb %s", kb_id, exc_info=True)
        raise
    finally:
        await client.close()
