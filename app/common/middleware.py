import contextvars
import time
import uuid

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

_request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="unknown")
_request_started_at_ctx: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "request_started_at", default=None
)


def get_correlation_id() -> str:
    return _request_id_ctx.get()


def get_latency_ms() -> float | None:
    started_at = _request_started_at_ctx.get()
    if started_at is None:
        return None
    return round((time.monotonic() - started_at) * 1000, 2)


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = str(uuid.uuid4())
        request_id_token = _request_id_ctx.set(request_id)

        request.state.request_id = request_id
        start = time.monotonic()
        started_at_token = _request_started_at_ctx.set(start)

        try:
            response = await call_next(request)
        finally:
            _request_id_ctx.reset(request_id_token)
            _request_started_at_ctx.reset(started_at_token)

        latency_ms = int((time.monotonic() - start) * 1000)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Latency-MS"] = str(latency_ms)

        return response
