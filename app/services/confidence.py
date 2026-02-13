from __future__ import annotations

from datetime import datetime, timezone

from app.config import (
    DEPTH_CONFIG,
    KNOWN_DEX_ROUTERS,
    KNOWN_DISTRIBUTOR_CONTRACTS,
    WRAPPED_TOKENS,
    LP_SYMBOLS,
)


def parse_iso(ts: str) -> datetime:
    """Parse an ISO timestamp string (with trailing Z) to a UTC datetime."""
    return datetime.fromisoformat(ts.rstrip("Z")).replace(tzinfo=timezone.utc)


# ============================================================
# Flag Detection
# ============================================================


def detect_flags(
    balance: dict,
    value_usd: float | None,
    first_seen: dict,
    recent_transfers: dict,
    token_info: dict,
    chain: str,
) -> list[str]:
    flags: list[str] = []

    if float(balance["formatted"]) == 0:
        return ["zero_balance"]

    # Value-based
    if value_usd is not None:
        if value_usd < 1.0:
            flags.append("dust_amount")
        if value_usd > 10000:
            flags.append("large_holder")

    # Time-dependent (medium/high confidence only)
    if first_seen.get("timestamp") and first_seen.get("confidence") in ("medium", "high"):
        days = (datetime.now(timezone.utc) - parse_iso(first_seen["timestamp"])).days
        if days < 7:
            flags.append("recently_acquired")

    # Transfer patterns
    inbound = recent_transfers.get("inbound", [])
    outbound = recent_transfers.get("outbound", [])
    in_count = len(inbound)
    out_count = len(outbound)

    if in_count == 1 and out_count == 0:
        flags.append("single_transfer_in")
    elif in_count >= 3:
        flags.append("multiple_inflows")

    if in_count + out_count >= 10:
        flags.append("frequent_trader")

    # DEX router source — precomputed lowercase sets from config
    routers = KNOWN_DEX_ROUTERS.get(chain, set())
    if routers and any(t.get("from", "").lower() in routers for t in inbound):
        flags.append("dex_router_source")

    # Possible airdrop
    if in_count == 1:
        from_addr = inbound[0].get("from", "").lower()
        if from_addr and from_addr in KNOWN_DISTRIBUTOR_CONTRACTS:
            flags.append("possible_airdrop")

    # Token type
    token_addr = token_info.get("address", "").lower()
    if token_addr in WRAPPED_TOKENS.get(chain, set()):
        flags.append("wrapped_token")
    if token_info.get("symbol", "").upper() in LP_SYMBOLS:
        flags.append("lp_token")

    return flags


# ============================================================
# Flag Scope Metadata
# ============================================================


def build_flag_scope(chain: str, depth: str, scan_stats: dict) -> dict:
    if chain == "base":
        return {
            "type": "block_window",
            "blocksScanned": scan_stats.get("blocks_scanned", 0),
            "approxDays": DEPTH_CONFIG[depth]["base_days"],
            "depth": depth,
        }
    elif chain == "solana":
        return {
            "type": "signature_window",
            "signaturesScanned": scan_stats.get("sigs_scanned", 0),
            "txParsed": scan_stats.get("tx_parsed", 0),
            "depth": depth,
        }
    return {"type": "unknown", "depth": depth}


# ============================================================
# Notes Generator (Rule-Based)
# ============================================================


def generate_notes(
    flags: list[str],
    first_seen: dict,
    recent_transfers: dict,
    balance: dict,
) -> list[str]:
    notes: list[str] = []

    if "zero_balance" in flags:
        return ["Wallet currently holds zero of this token"]

    if "multiple_inflows" in flags:
        notes.append("Position built over time across multiple transactions, not a single buy")
    if "single_transfer_in" in flags:
        notes.append("Entire position acquired in a single transaction (within scanned window)")
    if "possible_airdrop" in flags:
        notes.append("Position appears to have been received via airdrop distribution")
    if "recently_acquired" in flags and first_seen.get("timestamp"):
        days = (datetime.now(timezone.utc) - parse_iso(first_seen["timestamp"])).days
        notes.append(f"Token acquired approximately {days} days ago")
    if "frequent_trader" in flags:
        notes.append("High transfer frequency — this wallet actively trades this token")
    if "dex_router_source" in flags:
        notes.append("Token acquired via DEX swap — holding duration based on on-chain receipt")

    if first_seen.get("confidence") == "low":
        notes.append("Holding duration estimate has low confidence — scan window may not cover full history")

    # Net flow analysis
    total_out = sum(float(t["amount"]) for t in recent_transfers.get("outbound", []))
    if total_out > 0 and float(balance["formatted"]) > 0:
        notes.append("Net inflow exceeds current balance — some tokens were transferred out")

    if recent_transfers.get("truncated"):
        notes.append("Transfer history was truncated due to scan limits — partial view only")

    if "lp_token" in flags:
        notes.append("This is a liquidity pool token — value depends on underlying pool assets")
    if "wrapped_token" in flags:
        notes.append("This is a wrapped version of a native asset")

    return notes
