import asyncio
import logging
from datetime import datetime, timezone

from tools.registry import tool
from config.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


@tool(
    description="Search knowledge base documents for relevant information. Returns text chunks with source metadata (filename, page, section).",
    requires_hitl=False,
    connector="__kb__",
)
async def knowledge_base_search(
    query: str,
    kb_ids: list,
    user_id: str,
    top_k: int = 5,
) -> dict:
    """
    kb_ids and user_id are server-injected — LLM only provides query and optional top_k.
    Returns chunks with filename, page, section, chunk_type, excerpt, relevance_score.
    """
    if not kb_ids:
        return {"results": [], "message": "No knowledge bases assigned to this agent"}

    from document_pipeline.embedder import embed_texts
    from document_pipeline import retriever, reranker
    from database.session import get_custom_db_context_session
    from app.api_keys.db_models import UserApiKey
    from app.connectors.encryption import decrypt_str
    from sqlalchemy import select
    from uuid import UUID

    # Fetch user's OpenAI API key from DB
    async with get_custom_db_context_session() as db:
        key_result = await db.scalar(
            select(UserApiKey).where(UserApiKey.user_id == UUID(user_id)).limit(1)
        )
        if not key_result:
            return {"results": [], "error": "No API key found for user"}
        api_key = decrypt_str(key_result.encrypted_key)

    # Embed the query
    embeddings = await embed_texts([query], api_key)
    query_embedding = embeddings[0]

    # Search each KB in parallel
    search_tasks = [
        retriever.retrieve(kb_id, query_embedding, query)
        for kb_id in kb_ids
    ]
    per_kb_results = await asyncio.gather(*search_tasks, return_exceptions=True)

    # Merge all results — combine and re-sort by rrf_score
    merged: list[dict] = []
    for result in per_kb_results:
        if isinstance(result, Exception):
            logger.error("KB search failed for one KB: %s", result)
            continue
        merged.extend(result)

    merged.sort(key=lambda x: x.get("rrf_score", 0), reverse=True)

    # Rerank — pass top_k so reranker returns the right number, not a second slice
    if merged:
        merged = await reranker.rerank(query, merged, api_key, top_k=top_k)

    # Format output with source metadata
    results = []
    for item in merged:
        text = item.get("text", "")
        score = item.get("rerank_score", item.get("rrf_score", 0))
        results.append({
            "text": text,
            "filename": item.get("filename", ""),
            "kb_id": item.get("kb_id", ""),
            "doc_id": item.get("doc_id", ""),
            "page": item.get("page_start", 1),
            "section": item.get("section"),
            "chunk_type": item.get("chunk_type", "text"),
            "excerpt": text[:300] + "..." if len(text) > 300 else text,
            "relevance_score": round(float(score), 4),
        })

    return {
        "results": results,
        "query": query,
        "kb_ids_searched": kb_ids,
        "sources": [
            {
                "type": "knowledge_base",
                "title": r["filename"],
                "page": r.get("page"),
                "kb_id": r.get("kb_id"),
                "doc_id": r.get("doc_id"),
            }
            for r in results if r.get("filename")
        ],
    }
