from __future__ import annotations

import json
import time
import logging
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import RATE_LIMITS

logger = logging.getLogger("apix")

# Module-level hits store — shared across middleware rebuilds, clearable from tests
_hits: dict[str, list[float]] = defaultdict(list)


def reset_rate_limits():
    """Clear all rate limit state. Used by tests."""
    _hits.clear()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    In-memory sliding-window rate limiter.
    Two buckets:
      - per-IP: global request limit (default 60/min)
      - per-wallet+token: prevents re-querying the same position (default 10/min)
    """

    async def dispatch(self, request: Request, call_next):
        # Only rate-limit POST requests to the receipt endpoint
        if request.method != "POST" or "/v1/position-receipt/" not in request.url.path:
            return await call_next(request)

        now = time.monotonic()
        client_ip = request.client.host if request.client else "unknown"

        # --- Per-IP limit ---
        ip_key = f"ip:{client_ip}"
        ip_cfg = RATE_LIMITS["per_ip"]
        if _is_limited(ip_key, now, ip_cfg["max_requests"], ip_cfg["window_s"]):
            logger.warning("Rate limited (per-IP): %s", client_ip)
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "message": f"Too many requests. Limit: {ip_cfg['max_requests']} per {ip_cfg['window_s']}s.",
                },
                headers={"Retry-After": str(ip_cfg["window_s"])},
            )

        # --- Per-wallet+token limit (requires reading body) ---
        wallet = ""
        token = ""
        wt_key = ""
        try:
            raw = await request.body()
            body = json.loads(raw)
            # Handle APIX nested body
            inner = body.get("body") if isinstance(body.get("body"), dict) else body
            wallet = inner.get("address") or inner.get("wallet") or inner.get("addr") or ""
            token = inner.get("token") or inner.get("mint") or inner.get("contract") or ""

            if wallet and token:
                chain = request.url.path.rstrip("/").split("/")[-1]
                wt_key = f"wt:{chain}:{wallet.lower()}:{token.lower()}"
                wt_cfg = RATE_LIMITS["per_wallet_token"]
                if _is_limited(wt_key, now, wt_cfg["max_requests"], wt_cfg["window_s"]):
                    logger.warning("Rate limited (per-wallet+token): %s %s", wallet[:10], token[:10])
                    return JSONResponse(
                        status_code=429,
                        content={
                            "error": "rate_limited",
                            "message": f"Too many requests for this wallet+token pair. Limit: {wt_cfg['max_requests']} per {wt_cfg['window_s']}s.",
                        },
                        headers={"Retry-After": str(wt_cfg["window_s"])},
                    )
        except Exception as e:
            logger.debug("Rate limiter body parse failed (validation will catch): %s", e)

        # Record hits
        _record(ip_key, now)
        if wallet and token:
            _record(wt_key, now)

        return await call_next(request)


def _is_limited(key: str, now: float, max_requests: int, window_s: int) -> bool:
    """Check if the key has exceeded max_requests within the sliding window."""
    _prune(key, now, window_s)
    return len(_hits[key]) >= max_requests


def _record(key: str, now: float) -> None:
    """Record a hit timestamp."""
    _hits[key].append(now)


def _prune(key: str, now: float, window_s: int) -> None:
    """Remove expired entries outside the sliding window."""
    cutoff = now - window_s
    _hits[key] = [t for t in _hits[key] if t > cutoff]
