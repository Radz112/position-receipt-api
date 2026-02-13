import json
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger("apix")


class ApixMiddleware(BaseHTTPMiddleware):
    """
    Combined APIX middleware: request logging + body unwrapping.
    Single middleware avoids body-read issues with stacked BaseHTTPMiddleware.
    """

    async def dispatch(self, request: Request, call_next):
        if request.method == "POST":
            try:
                raw = await request.body()

                # 1. Log the raw request
                logger.info(
                    "APIX REQUEST | path=%s | content-type=%s | body=%s",
                    request.url.path,
                    request.headers.get("content-type"),
                    raw.decode("utf-8", errors="replace")[:2000],
                )

                # 2. Unwrap body.body nesting if present
                body = json.loads(raw)
                if isinstance(body.get("body"), dict):
                    request._body = json.dumps(body["body"]).encode()
                    logger.debug("APIX body unwrapped: nested body detected")
            except Exception as e:
                logger.debug("APIX body processing failed: %s", e)

        return await call_next(request)
