from __future__ import annotations

from fastapi.responses import JSONResponse


def error_response(status: int, error: str, message: str, body: dict | None = None) -> JSONResponse:
    """
    All error responses include received_body for debugging APIX agent payload shape.
    """
    return JSONResponse(
        status_code=status,
        content={
            "error": error,
            "message": message,
            "received_body": {
                k: str(v)[:200] for k, v in (body or {}).items()
            },
        },
    )
