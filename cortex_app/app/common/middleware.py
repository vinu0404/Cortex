import contextvars
import time
import uuid

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

_request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="unknown")


def get_correlation_id() -> str:
    return _request_id_ctx.get()


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = str(uuid.uuid4())
        token = _request_id_ctx.set(request_id)

        request.state.request_id = request_id
        start = time.monotonic()

        try:
            response = await call_next(request)
        finally:
            _request_id_ctx.reset(token)

        latency_ms = int((time.monotonic() - start) * 1000)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Latency-MS"] = str(latency_ms)

        return response
