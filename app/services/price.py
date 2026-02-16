from __future__ import annotations

import asyncio
import logging
import time

from app.config import JUPITER_API_KEY
from app.services.rpc import get_client

logger = logging.getLogger("apix")

CIRCUIT_OPEN_DURATION = 60

_circuit: dict[str, dict] = {
    "jupiter": {"open": False, "until": 0},
    "dexscreener": {"open": False, "until": 0},
}
_price_cache: dict[str, dict] = {}

_PROVIDERS = {
    "solana": "jupiter",
    "base": "dexscreener",
}


def _circuit_open(provider: str) -> bool:
    cb = _circuit[provider]
    if cb["open"] and time.time() < cb["until"]:
        return True
    cb["open"] = False
    return False


def _trip_circuit(provider: str):
    _circuit[provider] = {"open": True, "until": time.time() + CIRCUIT_OPEN_DURATION}


async def get_token_price_cached(chain: str, token_address: str) -> float | None:
    key = f"{chain}:{token_address.lower()}"
    now = time.time()
    cached = _price_cache.get(key)
    if cached and now < cached["expires"]:
        return cached["price"]

    try:
        price = await asyncio.wait_for(_fetch_price(chain, token_address), timeout=0.3)
    except Exception as e:
        logger.debug("Price fetch failed for %s:%s — %s", chain, token_address, e)
        return None

    if price is not None:
        _price_cache[key] = {"price": price, "expires": now + 30}
    return price


async def _fetch_price(chain: str, token_address: str) -> float | None:
    provider = _PROVIDERS.get(chain)
    if not provider or _circuit_open(provider):
        return None

    client = get_client()
    try:
        if chain == "solana":
            headers = {"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {}
            resp = await client.get(
                f"https://api.jup.ag/price/v2?ids={token_address}",
                headers=headers,
            )
            if resp.status_code in (401, 429):
                _trip_circuit(provider)
                return None
            data = resp.json().get("data", {}).get(token_address)
            return float(data["price"]) if data and data.get("price") else None

        elif chain == "base":
            resp = await client.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}")
            if resp.status_code == 429:
                _trip_circuit(provider)
                return None
            pairs = resp.json().get("pairs")
            return float(pairs[0].get("priceUsd", 0)) if pairs else None
    except Exception as e:
        logger.warning("Price API error for %s:%s — %s", chain, token_address, e)
        return None
    return None
