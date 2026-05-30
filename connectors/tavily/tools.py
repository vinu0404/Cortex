import logging

import httpx

from config.settings import get_settings
from tools.registry import tool

settings = get_settings()
logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.tavily.com/search"
_EXTRACT_URL = "https://api.tavily.com/extract"


@tool(description="Search the web using Tavily Search API", requires_hitl=False, connector="")
async def web_search(
    query: str,
    num_results: int = 5,
    search_depth: str = "basic",
    include_answer: bool = True,
    topic: str = "general",
) -> dict:
    api_key = settings.TAVILY_API_KEY
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY not configured on server")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            _SEARCH_URL,
            json={
                "api_key": api_key,
                "query": query,
                "search_depth": search_depth,
                "include_answer": include_answer,
                "max_results": min(num_results, 20),
                "topic": topic,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    results = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", ""),
            "score": r.get("score", 0.0),
        }
        for r in data.get("results", [])
    ]
    return {
        "answer": data.get("answer"),
        "results": results,
        "query": data.get("query", query),
        "sources": [{"type": "web", "title": r["title"], "url": r["url"]} for r in results if r.get("url")],
    }


@tool(description="Search recent news using Tavily", requires_hitl=False, connector="")
async def web_search_news(query: str, num_results: int = 5) -> dict:
    return await web_search(query=query, num_results=num_results, topic="news")


@tool(description="Fetch and extract clean text content from a URL via Tavily", requires_hitl=False, connector="")
async def fetch_url(url: str) -> dict:
    api_key = settings.TAVILY_API_KEY
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY not configured on server")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            _EXTRACT_URL,
            json={"api_key": api_key, "urls": [url]},
        )
        resp.raise_for_status()
        data = resp.json()
    results = data.get("results", [])
    return {
        "url": url,
        "content": results[0].get("raw_content", "") if results else "",
        "sources": [{"type": "web", "title": url, "url": url}],
    }
