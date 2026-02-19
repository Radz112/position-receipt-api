from __future__ import annotations

import asyncio
import logging

from app.services import rpc
from app.services.rpc import get_client

logger = logging.getLogger("apix")

# Persistent metadata cache: (chain, address) -> metadata dict
_metadata_cache: dict[tuple[str, str], dict] = {}

# Local registry — top tokens per chain (bootstrap, avoids on-chain calls)
_LOCAL_REGISTRY: dict[tuple[str, str], dict] = {
    # Base
    ("base", "0x4ed4e862860bed51a9570b96d89af5e1b0efefed"): {
        "symbol": "DEGEN",
        "name": "Degen",
        "decimals": 18,
        "logo": None,
    },
    ("base", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"): {
        "symbol": "USDC",
        "name": "USD Coin",
        "decimals": 6,
        "logo": None,
    },
    ("base", "0x50c5725949a6f0c72e6c4a641f24049a917db0cb"): {
        "symbol": "DAI",
        "name": "Dai Stablecoin",
        "decimals": 18,
        "logo": None,
    },
    ("base", "0x4200000000000000000000000000000000000006"): {
        "symbol": "WETH",
        "name": "Wrapped Ether",
        "decimals": 18,
        "logo": None,
    },
    ("base", "0x0000000000000000000000000000000000000000"): {
        "symbol": "ETH",
        "name": "Ether",
        "decimals": 18,
        "logo": None,
    },
    # Solana
    ("solana", "so11111111111111111111111111111111111111112"): {
        "symbol": "SOL",
        "name": "Solana",
        "decimals": 9,
        "logo": None,
    },
    ("solana", "epjfwdd5aufqssqem2qn1xzybapc8g4weggkzwytdt1v"): {
        "symbol": "USDC",
        "name": "USD Coin",
        "decimals": 6,
        "logo": None,
    },
    ("solana", "dezzxtb7gnmweqrtzijhm3gc3bprbktajyjntrzg1v5"): {
        "symbol": "BONK",
        "name": "Bonk",
        "decimals": 5,
        "logo": None,
    },
    ("solana", "jup6lkbzbjc6cdoftefzqzpn1ccg6xaggfcpeymqhvj"): {
        "symbol": "JUP",
        "name": "Jupiter",
        "decimals": 6,
        "logo": None,
    },
}

# Reverse lookup: (chain, SYMBOL) -> address — built from _LOCAL_REGISTRY
_SYMBOL_TO_ADDRESS: dict[tuple[str, str], str] = {}
for (_chain, _addr), _meta in _LOCAL_REGISTRY.items():
    _SYMBOL_TO_ADDRESS[(_chain, _meta["symbol"].upper())] = _addr


async def resolve_symbol_to_address(chain: str, symbol: str) -> str | None:
    """Resolve a ticker symbol to a contract address.
    Checks local registry first, then DexScreener search.
    Returns the address or None if unresolvable.
    """
    key = (chain, symbol.upper())
    if key in _SYMBOL_TO_ADDRESS:
        return _SYMBOL_TO_ADDRESS[key]

    # Fallback: search DexScreener
    try:
        client = get_client()
        resp = await client.get(
            f"https://api.dexscreener.com/latest/dex/search?q={symbol}",
            timeout=3.0,
        )
        if resp.status_code == 200:
            pairs = resp.json().get("pairs") or []
            chain_id = "base" if chain == "base" else "solana"
            for pair in pairs:
                if pair.get("chainId") != chain_id:
                    continue
                base = pair.get("baseToken", {})
                if base.get("symbol", "").upper() == symbol.upper() and base.get("address"):
                    addr = base["address"]
                    # Cache for future lookups
                    _SYMBOL_TO_ADDRESS[key] = addr
                    return addr
    except Exception as e:
        logger.debug("DexScreener symbol search failed for %s: %s", symbol, e)

    return None


# ERC20 function selectors
NAME_SELECTOR = "0x06fdde03"
SYMBOL_SELECTOR = "0x95d89b41"
DECIMALS_SELECTOR = "0x313ce567"


def _decode_string(hex_data: str) -> str:
    """Decode ABI-encoded string from eth_call result.
    Handles both standard ABI-encoded strings and bytes32 returns.
    """
    if not hex_data or hex_data == "0x":
        return ""
    try:
        data = bytes.fromhex(hex_data[2:])
        if len(hex_data) >= 130:
            # Standard ABI-encoded string: offset + length + data
            offset = int.from_bytes(data[:32], "big")
            length = int.from_bytes(data[offset : offset + 32], "big")
            return data[offset + 32 : offset + 32 + length].decode("utf-8", errors="replace").strip("\x00")
        # bytes32 return (e.g. MKR-style tokens): right-padded with zeroes
        return data.rstrip(b"\x00").decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


async def resolve_token(chain: str, address: str) -> dict:
    """
    Resolve token metadata.
    Check local registry → persistent cache → on-chain fallback.
    Cache forever (token metadata doesn't change).
    Hard fails if metadata unreachable.
    """
    key = (chain, address.lower())

    # 1. Check local registry
    if key in _LOCAL_REGISTRY:
        return {**_LOCAL_REGISTRY[key], "address": address}

    # 2. Check persistent cache
    if key in _metadata_cache:
        return _metadata_cache[key]

    # 3. On-chain fallback
    if chain == "base":
        meta = await _resolve_evm(address)
    elif chain == "solana":
        meta = await _resolve_solana(address)
    else:
        raise ValueError(f"Unsupported chain: {chain}")

    meta["address"] = address
    _metadata_cache[key] = meta
    return meta


async def _resolve_evm(address: str) -> dict:
    """Fetch ERC20 metadata on-chain: name(), symbol(), decimals().
    Falls back to DexScreener if on-chain calls return no symbol.
    """
    name_hex = await rpc.eth_call(address, NAME_SELECTOR)
    symbol_hex = await rpc.eth_call(address, SYMBOL_SELECTOR)
    decimals_hex = await rpc.eth_call(address, DECIMALS_SELECTOR)

    name = _decode_string(name_hex)
    symbol = _decode_string(symbol_hex)
    decimals = int(decimals_hex, 16) if decimals_hex and decimals_hex != "0x" else 18

    if not symbol:
        # Fallback: try DexScreener for metadata
        dex_meta = await _fetch_dexscreener_metadata(address)
        if dex_meta:
            dex_meta.setdefault("decimals", decimals)
            return dex_meta
        raise Exception(f"Could not resolve token metadata for {address} — no symbol returned")

    return {
        "symbol": symbol,
        "name": name or symbol,
        "decimals": decimals,
        "logo": None,
    }


async def _fetch_dexscreener_metadata(address: str) -> dict | None:
    """Fetch token metadata from DexScreener as a fallback."""
    try:
        client = get_client()
        resp = await client.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{address}",
            timeout=3.0,
        )
    except Exception as e:
        logger.debug("DexScreener metadata fallback failed for %s: %s", address, e)
        return None

    if resp.status_code != 200:
        return None

    pairs = resp.json().get("pairs")
    if not pairs:
        return None

    # Use baseToken info from the first pair where this address is the base token
    for pair in pairs:
        base = pair.get("baseToken", {})
        if base.get("address", "").lower() == address.lower() and base.get("symbol"):
            return {
                "symbol": base["symbol"],
                "name": base.get("name", base["symbol"]),
                "decimals": None,  # DexScreener doesn't provide decimals
                "logo": None,
            }

    return None


async def _resolve_solana(mint: str) -> dict:
    """
    Fetch SPL token metadata.
    Jupiter Token API for listed tokens (symbol, name, decimals, logo).
    On-chain getAccountInfo for unlisted tokens (decimals only).
    """
    jupiter_meta, onchain_decimals = await asyncio.gather(
        _fetch_jupiter_token_metadata(mint),
        _fetch_solana_onchain_decimals(mint),
    )

    if jupiter_meta:
        # Jupiter provides complete metadata; prefer its decimals but fall back to on-chain
        jupiter_meta.setdefault("decimals", onchain_decimals)
        return jupiter_meta

    if onchain_decimals is None:
        raise Exception(f"Could not resolve Solana token {mint} — account not found")

    return {
        "symbol": mint[:6] + "...",
        "name": mint[:6] + "...",
        "decimals": onchain_decimals,
        "logo": None,
    }


async def _fetch_jupiter_token_metadata(mint: str) -> dict | None:
    """Fetch token metadata from Jupiter Token API. Returns None if unlisted or unreachable."""
    try:
        client = get_client()
        resp = await client.get(
            f"https://tokens.jup.ag/token/{mint}",
            timeout=3.0,
        )
    except Exception as e:
        logger.debug("Jupiter token API unreachable for %s: %s", mint, e)
        return None

    if resp.status_code != 200:
        return None

    data = resp.json()
    symbol = data.get("symbol")
    if not symbol:
        return None

    return {
        "symbol": symbol,
        "name": data.get("name", symbol),
        "decimals": data.get("decimals"),
        "logo": data.get("logoURI"),
    }


async def _fetch_solana_onchain_decimals(mint: str) -> int | None:
    """Fetch decimals from the on-chain mint account. Returns None if account not found."""
    result = await rpc.solana_rpc(
        "getAccountInfo",
        [mint, {"encoding": "jsonParsed"}],
    )

    if not result or not result.get("value"):
        return None

    account_data = result["value"]["data"]
    if isinstance(account_data, dict) and account_data.get("parsed"):
        parsed = account_data["parsed"]
        if parsed.get("type") == "mint":
            return parsed["info"].get("decimals", 0)

    return 0
