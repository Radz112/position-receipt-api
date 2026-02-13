from __future__ import annotations

import httpx
import logging

from app.config import BASE_RPC_URL, SOLANA_RPC_URL

logger = logging.getLogger("apix")

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

async def _eth_rpc(method: str, params: list):
    """Execute a Base JSON-RPC call and return the result field."""
    resp = await get_client().post(
        BASE_RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise Exception(f"RPC error: {data['error']}")
    return data["result"]


async def solana_rpc(method: str, params: list):
    """Execute a Solana JSON-RPC call and return the result field."""
    resp = await get_client().post(
        SOLANA_RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise Exception(f"Solana RPC error: {data['error']}")
    return data["result"]


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
