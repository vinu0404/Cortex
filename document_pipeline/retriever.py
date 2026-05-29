import asyncio
import logging

from config.settings import get_settings
from document_pipeline import vector_store

settings = get_settings()
logger = logging.getLogger(__name__)


async def retrieve(
    kb_id: str,
    query_embedding: list[float],
    query: str,
) -> list[dict]:
    """
    Hybrid search: dense (Qdrant vector) + keyword (Qdrant text index) + RRF merge.
    Returns top KB_TOP_K_RRF results with rrf_score, payload fields.
    """
    dense_results, text_results = await asyncio.gather(
        vector_store.dense_search(kb_id, query_embedding, settings.KB_TOP_K_DENSE),
        vector_store.text_search(kb_id, query, settings.KB_TOP_K_SPARSE),
    )
    merged = _rrf_merge(dense_results, text_results, settings.KB_TOP_K_RRF, settings.KB_RRF_K)
    return merged


def _rrf_merge(dense: list[dict], sparse: list[dict], top_k: int, k: int) -> list[dict]:
    """Reciprocal Rank Fusion: score(doc) = Σ 1 / (k + rank)"""
    scores: dict[str, float] = {}
    payload_map: dict[str, dict] = {}

    for rank, item in enumerate(dense):
        point_id = item["id"]
        scores[point_id] = scores.get(point_id, 0.0) + 1.0 / (k + rank + 1)
        payload_map[point_id] = item["payload"]

    for rank, item in enumerate(sparse):
        point_id = item["id"]
        scores[point_id] = scores.get(point_id, 0.0) + 1.0 / (k + rank + 1)
        payload_map[point_id] = item["payload"]

    sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)[:top_k]
    return [
        {
            "id": pid,
            "rrf_score": scores[pid],
            "text": payload_map[pid].get("text", ""),
            "filename": payload_map[pid].get("filename", ""),
            "doc_id": payload_map[pid].get("doc_id", ""),
            "page_start": payload_map[pid].get("page_start", 1),
            "page_end": payload_map[pid].get("page_end", 1),
            "section": payload_map[pid].get("section"),
            "chunk_type": payload_map[pid].get("chunk_type", "text"),
        }
        for pid in sorted_ids
    ]
