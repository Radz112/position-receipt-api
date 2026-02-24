from __future__ import annotations

import asyncio
import httpx
import logging

from app.config import BASE_RPC_URL, SOLANA_RPC_URL

logger = logging.getLogger("apix")

_MAX_RETRIES = 3
_RETRY_BACKOFF = 0.25  # seconds; doubles each retry (0.25, 0.5, 1.0)
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

async def _rpc_with_retry(url: str, payload: dict, label: str):
    """Execute a JSON-RPC call with retry on transient failures."""
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = await get_client().post(url, json=payload)
            if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                wait = _RETRY_BACKOFF * (2 ** attempt)
                logger.info(
                    "%s returned %d, retrying in %.2fs (attempt %d/%d)",
                    label, resp.status_code, wait, attempt + 1, _MAX_RETRIES,
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
        except httpx.TimeoutException as e:
            last_exc = e
            if attempt < _MAX_RETRIES:
                wait = _RETRY_BACKOFF * (2 ** attempt)
                logger.info(
                    "%s timed out, retrying in %.2fs (attempt %d/%d)",
                    label, wait, attempt + 1, _MAX_RETRIES,
                )
                await asyncio.sleep(wait)
                continue
            raise
    raise last_exc  # type: ignore[misc]


async def _eth_rpc(method: str, params: list):
    """Execute a Base JSON-RPC call and return the result field."""
    return await _rpc_with_retry(
        BASE_RPC_URL,
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        "RPC",
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


async def eth_get_logs(params: dict) -> list:
    return await _eth_rpc("eth_getLogs", [params])


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
