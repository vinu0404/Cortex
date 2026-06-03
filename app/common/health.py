import time
from typing import Any

from sqlalchemy import text

from app.common.redis_client import get_async_redis
from app.common.retry import async_redis_call
from database.session import get_custom_db_context_session


async def check_database() -> dict[str, Any]:
    start = time.perf_counter()
    try:
        async with get_custom_db_context_session() as db:
            await db.execute(text("SELECT 1"))
        return _dependency("postgresql", "ok", start)
    except Exception as exc:
        return _dependency("postgresql", "down", start, str(exc))


async def check_redis() -> dict[str, Any]:
    start = time.perf_counter()
    try:
        redis = get_async_redis()
        await async_redis_call(redis, "ping")
        return _dependency("redis", "ok", start)
    except Exception as exc:
        return _dependency("redis", "down", start, str(exc))


def readiness_payload(checks: list[dict[str, Any]]) -> dict[str, Any]:
    status = "down" if any(check["status"] == "down" for check in checks) else "ok"
    return {"status": status, "dependencies": checks}


def _dependency(name: str, status: str, start: float, error: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "status": status,
        "latency_ms": round((time.perf_counter() - start) * 1000, 2),
    }
    if error:
        payload["error"] = error
    return payload
