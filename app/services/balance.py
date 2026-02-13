from __future__ import annotations

from app.config import NATIVE_TOKENS
from app.services import rpc

BALANCE_OF_SELECTOR = "0x70a08231"
DECIMALS_SELECTOR = "0x313ce567"

_decimals_cache: dict[str, int] = {}


def _encode_address(address: str) -> str:
    return address.lower().replace("0x", "").zfill(64)


async def _get_decimals_base(token: str) -> int:
    key = token.lower()
    if key in _decimals_cache:
        return _decimals_cache[key]
    result = await rpc.eth_call(token, DECIMALS_SELECTOR)
    decimals = int(result, 16) if result and result != "0x" else 18
    _decimals_cache[key] = decimals
    return decimals


async def get_token_balance_base(address: str, token: str) -> dict:
    if token.lower() in NATIVE_TOKENS["base"]:
        balance_wei = await rpc.eth_get_balance(address)
        return {"raw": balance_wei, "decimals": 18, "formatted": str(balance_wei / 10**18)}

    data = BALANCE_OF_SELECTOR + _encode_address(address)
    result = await rpc.eth_call(token, data)
    balance_raw = int(result, 16) if result and result != "0x" else 0
    decimals = await _get_decimals_base(token)
    return {"raw": balance_raw, "decimals": decimals, "formatted": str(balance_raw / (10**decimals))}


async def get_token_balance_solana(address: str, mint: str) -> dict:
    if mint.lower() in NATIVE_TOKENS["solana"]:
        balance = await rpc.solana_get_balance(address)
        lamports = balance["value"]
        return {"raw": lamports, "decimals": 9, "formatted": str(lamports / 10**9)}

    accounts = await rpc.solana_get_token_accounts_by_owner(address, mint)
    if not accounts["value"]:
        return {"raw": 0, "decimals": 0, "formatted": "0"}

    total = sum(
        int(acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
        for acc in accounts["value"]
    )
    decimals = accounts["value"][0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["decimals"]
    return {"raw": total, "decimals": decimals, "formatted": str(total / (10**decimals))}


async def get_token_balance(chain: str, address: str, token: str) -> dict:
    if chain == "base":
        return await get_token_balance_base(address, token)
    elif chain == "solana":
        return await get_token_balance_solana(address, token)
    raise ValueError(f"Unsupported chain: {chain}")
