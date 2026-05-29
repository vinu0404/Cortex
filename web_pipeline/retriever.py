import asyncio
import logging

from web_pipeline import vector_store

logger = logging.getLogger(__name__)


async def retrieve(collection_id: str, query_embedding: list[float], top_k: int) -> list[dict]:
    results = await vector_store.dense_search(collection_id, query_embedding, top_k)
    return [
        {
            "score": r["score"],
            "text": r["payload"].get("text", ""),
            "url": r["payload"].get("url", ""),
            "title": r["payload"].get("title", ""),
            "depth": r["payload"].get("depth", 0),
        }
        for r in results
    ]


async def retrieve_multi(collection_ids: list[str], query_embedding: list[float], top_k: int) -> list[dict]:
    tasks = [retrieve(cid, query_embedding, top_k) for cid in collection_ids]
    per_coll = await asyncio.gather(*tasks, return_exceptions=True)
    merged = []
    for res in per_coll:
        if isinstance(res, Exception):
            logger.error("WC retrieve failed for one collection: %s", res)
            continue
        merged.extend(res)
    merged.sort(key=lambda x: x["score"], reverse=True)
    return merged[:top_k]
