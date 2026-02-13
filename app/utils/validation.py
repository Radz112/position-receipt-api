from __future__ import annotations

import re

from app.config import VALID_CHAINS, DEPTH_CONFIG

_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

VALID_DEPTHS = set(DEPTH_CONFIG)


def validate_chain(chain: str) -> str | None:
    if chain not in VALID_CHAINS:
        return f"Invalid chain '{chain}'. Must be one of: {', '.join(sorted(VALID_CHAINS))}"
    return None


def validate_address(chain: str, address: str) -> str | None:
    if not address:
        return "address is required"
    if chain == "base" and not _EVM_ADDRESS_RE.match(address):
        return f"Invalid Base address: {address}. Must be 0x-prefixed, 40 hex characters."
    if chain == "solana" and not _SOLANA_ADDRESS_RE.match(address):
        return f"Invalid Solana address: {address}. Must be base58, 32-44 characters."
    return None


def validate_token(chain: str, token: str) -> str | None:
    if not token:
        return "token is required"
    if token.lower() in ("eth", "sol"):
        return None
    if chain == "base" and not _EVM_ADDRESS_RE.match(token):
        return f"Invalid token address: {token}. Must be 0x-prefixed, 40 hex characters."
    if chain == "solana" and not _SOLANA_ADDRESS_RE.match(token):
        return f"Invalid token mint: {token}. Must be base58, 32-44 characters."
    return None


def validate_depth(depth: str) -> str | None:
    if depth not in VALID_DEPTHS:
        return f"Invalid depth '{depth}'. Must be one of: {', '.join(sorted(VALID_DEPTHS))}"
    return None
