from typing import Any

from fastapi.responses import JSONResponse

from app.common.middleware import get_correlation_id, get_latency_ms


def _base_envelope(status_code: int, message: str, data: Any, error: Any) -> dict[str, Any]:
    return {
        "success": error is None,
        "status_code": status_code,
        "message": message,
        "data": data,
        "error": error,
        "request_id": get_correlation_id(),
        "latency_ms": get_latency_ms(),
    }


def ok(data: Any = None, message: str = "success", status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=_base_envelope(status_code, message, data, None),
    )


def fail(code: str, message: str, status_code: int = 400, details: Any = None) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return JSONResponse(
        status_code=status_code,
        content=_base_envelope(status_code, message, None, error),
    )
