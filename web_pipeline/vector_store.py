import logging
import uuid

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

from config.settings import get_settings
from document_pipeline.chunker import Chunk

settings = get_settings()
logger = logging.getLogger(__name__)


def _client() -> AsyncQdrantClient:
    return AsyncQdrantClient(url=settings.QDRANT_URL)


def _collection_name(collection_id: str) -> str:
    return f"wc_{collection_id}"


async def ensure_collection(collection_id: str) -> None:
    client = _client()
    try:
        collections = await client.get_collections()
        existing = {c.name for c in collections.collections}
        name = _collection_name(collection_id)
        if name not in existing:
            await client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=settings.KB_EMBEDDING_DIMS, distance=Distance.COSINE),
            )
            logger.info("Created Qdrant WC collection: %s", name)
    finally:
        await client.close()


async def upsert_chunks(
    collection_id: str,
    url_id: str,
    chunks: list[Chunk],
    embeddings: list[list[float]],
    payloads: list[dict],
) -> None:
    client = _client()
    try:
        points = [
            PointStruct(
                id=str(uuid.uuid4()),
                vector=embeddings[i],
                payload={
                    "url_id": url_id,
                    "collection_id": collection_id,
                    "chunk_index": chunk.chunk_index,
                    "text": chunk.text,
                    "chunk_type": chunk.chunk_type,
                    "url": payloads[i].get("url", ""),
                    "title": payloads[i].get("title", ""),
                    "depth": payloads[i].get("depth", 0),
                },
            )
            for i, chunk in enumerate(chunks)
        ]
        await client.upsert(collection_name=_collection_name(collection_id), points=points)
        logger.info("Upserted %d WC points to collection %s", len(points), collection_id)
    finally:
        await client.close()


async def delete_url_chunks(collection_id: str, url_id: str) -> None:
    client = _client()
    try:
        await client.delete(
            collection_name=_collection_name(collection_id),
            points_selector=Filter(must=[FieldCondition(key="url_id", match=MatchValue(value=url_id))]),
        )
    except Exception as e:
        if getattr(e, "status_code", None) == 404:
            logger.warning("Qdrant WC collection not found when deleting chunks for url %s (collection %s) — skipping", url_id, collection_id)
        else:
            logger.error("Failed to delete WC chunks for url %s from collection %s", url_id, collection_id, exc_info=True)
    finally:
        await client.close()


async def delete_collection(collection_id: str) -> None:
    client = _client()
    try:
        await client.delete_collection(_collection_name(collection_id))
    except Exception:
        logger.error("Failed to delete Qdrant WC collection %s", collection_id, exc_info=True)
    finally:
        await client.close()


async def dense_search(collection_id: str, query_embedding: list[float], top_k: int) -> list[dict]:
    client = _client()
    try:
        results = await client.search(
            collection_name=_collection_name(collection_id),
            query_vector=query_embedding,
            limit=top_k,
            with_payload=True,
        )
        return [{"id": str(r.id), "score": r.score, "payload": r.payload} for r in results]
    except Exception:
        logger.error("WC dense search failed for collection %s", collection_id, exc_info=True)
        return []
    finally:
        await client.close()
