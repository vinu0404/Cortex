from typing import Any

from fastapi.responses import JSONResponse


def ok(data: Any = None, message: str = "success", status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"status": "ok", "message": message, "data": data},
    )


def fail(code: str, message: str, status_code: int = 400, details: Any = None) -> JSONResponse:
    content: dict[str, Any] = {"status": "error", "code": code, "message": message}
    if details is not None:
        content["details"] = details
    return JSONResponse(status_code=status_code, content=content)
