import asyncio
import json
import logging
from functools import partial

import litellm
from sentence_transformers import CrossEncoder


from config.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


# accept top_k from caller so it wins over KB_TOP_K_FINAL
async def rerank(query: str, candidates: list[dict], api_key: str, top_k: int | None = None) -> list[dict]:
    strategy = settings.KB_RERANK_STRATEGY
    effective_top_k = top_k if top_k is not None else settings.KB_TOP_K_FINAL

    if strategy == "cross_encoder":
        # cross-encoder is CPU-bound — run in executor to avoid blocking event loop
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, partial(_rerank_cross_encoder, query, candidates, effective_top_k)
        )
    if strategy == "llm":
        return await _rerank_llm(query, candidates, api_key, effective_top_k)
    return candidates[:effective_top_k]


def _rerank_cross_encoder(query: str, candidates: list[dict], top_k: int) -> list[dict]:
    try:
        model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
        pairs = [(query, c["text"]) for c in candidates]
        scores = model.predict(pairs)
        for candidate, score in zip(candidates, scores):
            candidate["rerank_score"] = float(score)
        return sorted(candidates, key=lambda x: x.get("rerank_score", 0), reverse=True)[:top_k]
    except ImportError:
        logger.error("sentence-transformers not installed; falling back to RRF order")
        return candidates[:top_k]
    except Exception:
        logger.error("Cross-encoder reranking failed; falling back to RRF order", exc_info=True)
        return candidates[:top_k]


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
