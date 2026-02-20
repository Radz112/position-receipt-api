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

_FALLBACK_PROVIDERS = {
    "solana": "dexscreener",
}


def _circuit_open(provider: str) -> bool:
    cb = _circuit[provider]
    if cb["open"] and time.time() < cb["until"]:
        return True
    cb["open"] = False
    return False


def _trip_circuit(provider: str):
    _circuit[provider] = {"open": True, "until": time.time() + CIRCUIT_OPEN_DURATION}


_NATIVE_PRICE_MAP = {
    ("base", "eth"): "0x4200000000000000000000000000000000000006",      # WETH on Base
    ("solana", "sol"): "So11111111111111111111111111111111111111112",     # Wrapped SOL
}


async def get_token_price_cached(chain: str, token_address: str) -> float | None:
    # Map native token symbols to addresses that price APIs recognize
    canonical = _NATIVE_PRICE_MAP.get((chain, token_address.lower()))
    if canonical:
        token_address = canonical

    key = f"{chain}:{token_address.lower()}"
    now = time.time()
    cached = _price_cache.get(key)
    if cached and now < cached["expires"]:
        return cached["price"]

    try:
        price = await asyncio.wait_for(_fetch_price(chain, token_address), timeout=3.0)
    except Exception as e:
        logger.debug("Price fetch failed for %s:%s — %s", chain, token_address, e)
        price = None

    # Fallback provider if primary failed
    if price is None and chain in _FALLBACK_PROVIDERS:
        try:
            price = await asyncio.wait_for(
                _fetch_price_dexscreener(token_address), timeout=3.0
            )
        except Exception as e:
            logger.debug("Price fallback failed for %s:%s — %s", chain, token_address, e)

    if price is not None:
        _price_cache[key] = {"price": price, "expires": now + 30}
    return price


_STABLECOIN_SYMBOLS = {"USDC", "USDT", "DAI", "BUSD", "USDbC", "USDC.e"}


def _extract_price_from_pairs(pairs: list, token_address: str) -> float | None:
    """Find the best price from DexScreener pairs for a given token.
    Prefers pairs where our token is base and quote is a stablecoin.
    """
    if not pairs:
        return None

    addr_lower = token_address.lower()
    best = None

    for pair in pairs:
        base = pair.get("baseToken", {})
        quote = pair.get("quoteToken", {})
        price_usd = pair.get("priceUsd")
        if not price_usd:
            continue

        # Our token is the base token — priceUsd is directly its price
        if base.get("address", "").lower() == addr_lower:
            price = float(price_usd)
            # Prefer stablecoin-quoted pairs (most accurate)
            if quote.get("symbol", "").upper() in _STABLECOIN_SYMBOLS:
                return price
            if best is None:
                best = price

    return best


async def _fetch_price_dexscreener(token_address: str) -> float | None:
    """Fetch price from DexScreener (works for any chain)."""
    if _circuit_open("dexscreener"):
        return None
    client = get_client()
    try:
        resp = await client.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}")
        if resp.status_code == 429:
            _trip_circuit("dexscreener")
            return None
        pairs = resp.json().get("pairs")
        return _extract_price_from_pairs(pairs, token_address)
    except Exception as e:
        logger.warning("DexScreener price error for %s — %s", token_address, e)
        return None


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
            return _extract_price_from_pairs(pairs, token_address)
    except Exception as e:
        logger.warning("Price API error for %s:%s — %s", chain, token_address, e)
        return None
    return None
