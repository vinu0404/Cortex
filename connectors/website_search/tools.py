import logging

from tools.registry import tool

logger = logging.getLogger(__name__)


@tool(
    description=(
        "Search your scraped website collections for relevant information. "
        "Use this to find content from websites you have already crawled and indexed."
    ),
    requires_hitl=False,
    connector="__website__",
)
async def collection_search(
    query: str,
    collection_ids: list,
    user_id: str,
    top_k: int = 5,
) -> dict:
    """
    collection_ids and user_id are server-injected — LLM only provides query and optional top_k.
    Returns chunks with url, title, text, depth, excerpt, relevance_score.
    """
    if not collection_ids:
        return {"results": [], "message": "No website collections assigned to this agent"}

    from document_pipeline.embedder import embed_texts
    from web_pipeline import retriever
    from database.session import get_custom_db_context_session
    from app.api_keys.db_models import UserApiKey
    from app.connectors.encryption import decrypt_str
    from sqlalchemy import select
    from uuid import UUID

    async with get_custom_db_context_session() as db:
        key_result = await db.scalar(
            select(UserApiKey).where(UserApiKey.user_id == UUID(user_id)).limit(1)
        )
        if not key_result:
            return {"results": [], "error": "No API key found for user"}
        api_key = decrypt_str(key_result.encrypted_key)

    embeddings = await embed_texts([query], api_key)
    query_embedding = embeddings[0]

    results_raw = await retriever.retrieve_multi(
        [str(cid) for cid in collection_ids],
        query_embedding,
        top_k * 3,
    )

    results = []
    for item in results_raw[:top_k]:
        text = item.get("text", "")
        results.append({
            "text": text,
            "url": item.get("url", ""),
            "title": item.get("title", ""),
            "depth": item.get("depth", 0),
            "excerpt": text[:300] + "..." if len(text) > 300 else text,
            "relevance_score": round(float(item.get("score", 0)), 4),
        })

    return {
        "results": results,
        "query": query,
        "collection_ids_searched": collection_ids,
        "sources": [
            {"type": "website", "title": r.get("title") or r["url"], "url": r["url"]}
            for r in results if r.get("url")
        ],
    }
