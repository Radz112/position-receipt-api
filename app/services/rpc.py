from __future__ import annotations

import asyncio
import httpx
import logging

from app.config import BASE_RPC_URL, BASE_RPC_FALLBACKS, SOLANA_RPC_URL

logger = logging.getLogger("apix")

_MAX_RETRIES = 3
_RETRY_BACKOFF = 0.15  # seconds; doubles each retry (0.15, 0.3, 0.6)
_RETRYABLE_STATUS = {429, 502, 503}

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
    return _client


async def close_client():
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


# --- Shared RPC helpers ---

async def _rpc_with_retry(
    url: str,
    payload: dict,
    label: str,
    fallback_urls: list[str] | None = None,
):
    """Execute a JSON-RPC call with retry + fallback RPC rotation."""
    urls = [url] + (fallback_urls or [])
    last_exc: Exception | None = None

    for attempt in range(_MAX_RETRIES + 1):
        target = urls[attempt % len(urls)]
        try:
            resp = await get_client().post(target, json=payload)
            if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                wait = _RETRY_BACKOFF * (2 ** attempt)
                next_url = urls[(attempt + 1) % len(urls)]
                logger.info(
                    "%s %s returned %d, retrying via %s in %.2fs (attempt %d/%d)",
                    label, target, resp.status_code, next_url, wait,
                    attempt + 1, _MAX_RETRIES,
                )
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise Exception(f"{label} error: {data['error']}")
            return data["result"]
        except httpx.HTTPStatusError:
            raise
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_exc = e
            if attempt < _MAX_RETRIES:
                wait = _RETRY_BACKOFF * (2 ** attempt)
                next_url = urls[(attempt + 1) % len(urls)]
                logger.info(
                    "%s %s failed (%s), retrying via %s in %.2fs (attempt %d/%d)",
                    label, target, type(e).__name__, next_url, wait,
                    attempt + 1, _MAX_RETRIES,
                )
                await asyncio.sleep(wait)
                continue
            raise
    raise last_exc  # type: ignore[misc]


async def _eth_rpc(method: str, params: list):
    """Execute a Base JSON-RPC call with fallback RPC rotation."""
    return await _rpc_with_retry(
        BASE_RPC_URL,
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        "RPC",
        fallback_urls=BASE_RPC_FALLBACKS,
    )


async def solana_rpc(method: str, params: list):
    """Execute a Solana JSON-RPC call and return the result field."""
    return await _rpc_with_retry(
        SOLANA_RPC_URL,
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        "Solana RPC",
    )


# --- Base (EVM) ---

async def eth_call(to: str, data: str, block: str = "latest") -> str:
    return await _eth_rpc("eth_call", [{"to": to, "data": data}, block])


async def eth_get_balance(address: str, block: str = "latest") -> int:
    result = await _eth_rpc("eth_getBalance", [address, block])
    return int(result, 16)


_FALLBACK_CHUNK_SIZE = 1000  # safe for all known free RPCs
_MIN_CHUNK = 200


def _is_range_error(exc: Exception) -> bool:
    s = str(exc).lower()
    return "range" in s and ("large" in s or "exceed" in s or "limit" in s)


async def eth_get_logs(params: dict) -> list:
    """eth_getLogs with automatic sub-chunking on 'range too large' errors."""
    try:
        return await _eth_rpc("eth_getLogs", [params])
    except Exception as e:
        if not _is_range_error(e):
            raise

    from_block = int(params["fromBlock"], 16)
    to_block = int(params["toBlock"], 16)
    span = to_block - from_block
    if span <= _MIN_CHUNK:
        raise  # already small, nothing to split

    sub_size = _FALLBACK_CHUNK_SIZE
    logger.info("eth_getLogs range too large (%d blocks), re-fetching in %d-block sub-chunks", span, sub_size)
    all_logs: list = []
    cursor = from_block
    while cursor <= to_block:
        sub_end = min(cursor + sub_size, to_block)
        sub_params = {**params, "fromBlock": hex(cursor), "toBlock": hex(sub_end)}
        sub_logs = await _eth_rpc("eth_getLogs", [sub_params])
        all_logs.extend(sub_logs)
        cursor = sub_end + 1
    return all_logs


async def eth_block_number() -> int:
    result = await _eth_rpc("eth_blockNumber", [])
    return int(result, 16)


async def eth_get_block_by_number(block: str, full_tx: bool = False) -> dict:
    return await _eth_rpc("eth_getBlockByNumber", [block, full_tx])


# --- Solana ---

async def solana_get_balance(address: str) -> dict:
    return await solana_rpc("getBalance", [address])


async def solana_get_token_accounts_by_owner(owner: str, mint: str) -> dict:
    return await solana_rpc(
        "getTokenAccountsByOwner",
        [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
    )


async def solana_get_signatures_for_address(address: str, limit: int = 1000, before: str | None = None) -> list:
    opts: dict = {"limit": min(limit, 1000)}
    if before:
        opts["before"] = before
    return await solana_rpc("getSignaturesForAddress", [address, opts])


async def solana_get_transaction(signature: str) -> dict | None:
    return await solana_rpc(
        "getTransaction",
        [signature, {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}],
    )
