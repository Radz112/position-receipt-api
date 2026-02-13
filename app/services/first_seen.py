from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from app.config import DEPTH_CONFIG
from app.services import rpc
from app.utils.evm import TRANSFER_TOPIC, pad_address

logger = logging.getLogger("apix")

CHUNK_SIZE = 50_000
BASE_AVG_BLOCK_TIME = 2.0


def _budget_exceeded(calls_used: int, max_calls: int, start_time: float, max_time: float) -> bool:
    return calls_used >= max_calls or (time.monotonic() - start_time) > max_time


# ============================================================
# Base (EVM): Chunked Binary-Narrowing Log Scan
# ============================================================


async def estimate_first_seen_base(address: str, token: str, depth: str = "standard") -> dict:
    config = DEPTH_CONFIG[depth]
    max_calls = config["max_rpc_calls"]
    max_time = config["max_time_s"]
    start_time = time.monotonic()
    calls_used = 0

    current_block = await rpc.eth_block_number()
    calls_used += 1

    target_timestamp = int(time.time()) - (config["base_days"] * 86400)
    scan_start_block = await _timestamp_to_block(target_timestamp, current_block)
    calls_used += 1

    padded_addr = pad_address(address)
    chunks = []
    cursor = scan_start_block
    while cursor < current_block:
        chunk_end = min(cursor + CHUNK_SIZE, current_block)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + 1

    earliest_block = None
    earliest_timestamp = None
    hit_cap = False

    for chunk_start, chunk_end in chunks:
        if _budget_exceeded(calls_used, max_calls, start_time, max_time):
            hit_cap = True
            break

        try:
            logs = await rpc.eth_get_logs({
                "address": token,
                "fromBlock": hex(chunk_start),
                "toBlock": hex(chunk_end),
                "topics": [TRANSFER_TOPIC, None, padded_addr],
            })
        except Exception as e:
            logger.warning("eth_getLogs failed for chunk %d-%d: %s", chunk_start, chunk_end, e)
            calls_used += 1
            continue
        calls_used += 1

        if logs:
            logs.sort(key=lambda l: int(l["blockNumber"], 16))
            earliest_block = int(logs[0]["blockNumber"], 16)

            # Optional narrowing
            if (
                not _budget_exceeded(calls_used + 1, max_calls, start_time, max_time)
                and (earliest_block - chunk_start) > 10_000
            ):
                mid_block = chunk_start + (earliest_block - chunk_start) // 2
                try:
                    sub_logs = await rpc.eth_get_logs({
                        "address": token,
                        "fromBlock": hex(chunk_start),
                        "toBlock": hex(mid_block),
                        "topics": [TRANSFER_TOPIC, None, padded_addr],
                    })
                    calls_used += 1
                    if sub_logs:
                        sub_logs.sort(key=lambda l: int(l["blockNumber"], 16))
                        earliest_block = int(sub_logs[0]["blockNumber"], 16)
                except Exception as e:
                    logger.warning("Narrowing sub-scan failed: %s", e)
                    calls_used += 1

            if not _budget_exceeded(calls_used, max_calls, start_time, max_time):
                earliest_timestamp = await _get_block_timestamp(earliest_block)
                calls_used += 1

            break

    scan_days = config["base_days"]

    if earliest_timestamp is None and earliest_block is None:
        confidence = "low"
        note = f"No Transfer events found within {scan_days}-day scan window"
        if hit_cap:
            note += " (scan budget exhausted)"
    elif earliest_block is not None and (earliest_block - scan_start_block) < 1000:
        confidence = "low"
        note = "First event found near scan window boundary — actual first receipt may be earlier"
    elif hit_cap:
        confidence = "low"
        note = "Scan budget exhausted — result may not reflect earliest receipt"
    else:
        confidence = "medium"
        note = f"Based on first Transfer event within {scan_days}-day window"

    return {
        "timestamp": earliest_timestamp.isoformat() + "Z" if earliest_timestamp else None,
        "confidence": confidence,
        "method": "chunked_log_scan",
        "scanWindow": f"{scan_days} days",
        "note": note,
    }


async def _timestamp_to_block(target_ts: int, current_block: int) -> int:
    block_data = await rpc.eth_get_block_by_number(hex(current_block), False)
    current_ts = int(block_data["timestamp"], 16)
    estimated_blocks_back = int((current_ts - target_ts) / BASE_AVG_BLOCK_TIME)
    return max(0, current_block - estimated_blocks_back)


async def _get_block_timestamp(block_number: int) -> datetime | None:
    try:
        block_data = await rpc.eth_get_block_by_number(hex(block_number), False)
        return datetime.fromtimestamp(int(block_data["timestamp"], 16), tz=timezone.utc)
    except Exception as e:
        logger.warning("Failed to get block timestamp for %d: %s", block_number, e)
        return None


# ============================================================
# Solana: Multi-Account Signature Scan with Hard Caps
# ============================================================


async def estimate_first_seen_solana(address: str, mint: str, depth: str = "standard") -> dict:
    config = DEPTH_CONFIG[depth]
    max_sigs = config["sol_sigs"]
    max_time = config["max_time_s"]
    start_time = time.monotonic()

    token_accounts = await rpc.solana_get_token_accounts_by_owner(address, mint)
    if not token_accounts["value"]:
        return {
            "timestamp": None, "confidence": "low",
            "method": "token_account_scan", "scanWindow": "0 accounts",
            "note": "No token account found for this mint",
        }

    earliest_time = None
    total_sigs_scanned = 0
    hit_cap = False
    accounts_scanned = 0

    for acc in token_accounts["value"]:
        if (time.monotonic() - start_time) > max_time:
            hit_cap = True
            break

        remaining_budget = max_sigs - total_sigs_scanned
        if remaining_budget <= 0:
            hit_cap = True
            break

        token_account_pubkey = acc["pubkey"]
        batch_limit = min(remaining_budget, 1000)
        try:
            signatures = await rpc.solana_get_signatures_for_address(token_account_pubkey, limit=batch_limit)
        except Exception as e:
            logger.warning("getSignaturesForAddress failed for %s: %s", token_account_pubkey, e)
            continue

        accounts_scanned += 1
        total_sigs_scanned += len(signatures)

        if signatures:
            batch_block_time = signatures[-1].get("blockTime")
            if batch_block_time is not None:
                if earliest_time is None or batch_block_time < earliest_time:
                    earliest_time = batch_block_time
            if len(signatures) >= batch_limit:
                hit_cap = True

    total_accounts = len(token_accounts["value"])

    if earliest_time is None:
        confidence = "low"
        note = "No transaction history found for token accounts"
    elif hit_cap:
        confidence = "low"
        note = f"Scan limit reached ({total_sigs_scanned} signatures across {accounts_scanned} accounts). Actual first receipt could be earlier."
    elif accounts_scanned == total_accounts and total_sigs_scanned < max_sigs:
        confidence = "high"
        note = f"Full history scanned across {accounts_scanned} token account(s)"
    else:
        confidence = "medium"
        note = f"Scanned {total_sigs_scanned} signatures across {accounts_scanned} account(s)"

    timestamp_str = None
    if earliest_time is not None:
        timestamp_str = datetime.fromtimestamp(earliest_time, tz=timezone.utc).isoformat() + "Z"

    return {
        "timestamp": timestamp_str, "confidence": confidence,
        "method": "token_account_scan",
        "scanWindow": f"{total_sigs_scanned} signatures / {accounts_scanned} accounts",
        "note": note,
    }


# ============================================================
# Dispatcher
# ============================================================


async def estimate_first_seen(chain: str, address: str, token: str, depth: str = "standard") -> dict:
    if chain == "base":
        return await estimate_first_seen_base(address, token, depth)
    elif chain == "solana":
        return await estimate_first_seen_solana(address, token, depth)
    return {
        "timestamp": None, "confidence": "low",
        "method": "none", "scanWindow": "0",
        "note": f"Unsupported chain: {chain}",
    }
