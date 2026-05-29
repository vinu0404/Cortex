import json
import logging

import litellm

from config.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


async def rerank(query: str, candidates: list[dict], api_key: str, top_k: int | None = None) -> list[dict]:
    effective_top_k = top_k if top_k is not None else settings.KB_TOP_K_FINAL
    if settings.KB_RERANK_STRATEGY == "llm":
        return await _rerank_llm(query, candidates, api_key, effective_top_k)
    return candidates[:effective_top_k]


async def _rerank_llm(query: str, candidates: list[dict], api_key: str, top_k: int) -> list[dict]:
    scored = []
    for candidate in candidates:
        prompt = (
            f"Score this passage's relevance to the query on a scale of 1-10.\n"
            f"Query: {query}\n"
            f"Passage: {candidate['text'][:500]}\n"
            f"Respond with only a JSON object: {{\"score\": <number>}}"
        )
        try:
            response = await litellm.acompletion(
                model=settings.DEFAULT_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=50,
                api_key=api_key,
            )
            score_data = json.loads(response.choices[0].message.content)
            candidate["rerank_score"] = score_data.get("score", 0)
        except Exception:
            logger.error("LLM reranking failed for candidate: %s", candidate.get("id"), exc_info=True)
            candidate["rerank_score"] = 0
        scored.append(candidate)
    return sorted(scored, key=lambda x: x.get("rerank_score", 0), reverse=True)[:top_k]
