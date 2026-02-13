from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from app.config import TRANSFER_BUDGET
from app.services import rpc
from app.utils.evm import TRANSFER_TOPIC, pad_address, unpad_address

logger = logging.getLogger("apix")

EMPTY_TRANSFERS = {"inbound": [], "outbound": [], "truncated": False}


# ============================================================
# Base: Backward-Chunked Log Fetch with Early Exit
# ============================================================


async def get_recent_transfers_base(
    address: str, token: str, decimals: int, limit: int = 5
) -> dict:
    budget = TRANSFER_BUDGET["base"]
    start_time = time.monotonic()
    calls_used = 0

    current_block = await rpc.eth_block_number()
    calls_used += 1

    padded_addr = pad_address(address)
    inbound: list[dict] = []
    outbound: list[dict] = []
    truncated = False
    cursor = current_block

    def over_budget():
        return calls_used >= budget["max_rpc_calls"] or (time.monotonic() - start_time) > budget["max_time_s"]

    while cursor > 0:
        if over_budget():
            truncated = True
            break
        if len(inbound) >= budget["target_inbound"] and len(outbound) >= budget["target_outbound"]:
            break

        chunk_start = max(0, cursor - budget["chunk_size"])

        if len(inbound) < budget["target_inbound"]:
            try:
                in_logs = await rpc.eth_get_logs({
                    "address": token,
                    "fromBlock": hex(chunk_start), "toBlock": hex(cursor),
                    "topics": [TRANSFER_TOPIC, None, padded_addr],
                })
                calls_used += 1
                inbound.extend(_parse_transfer_logs(in_logs, decimals, "in"))
            except Exception as e:
                logger.warning("Inbound log fetch failed: %s", e)
                calls_used += 1

        if len(outbound) < budget["target_outbound"]:
            if over_budget():
                truncated = True
                break
            try:
                out_logs = await rpc.eth_get_logs({
                    "address": token,
                    "fromBlock": hex(chunk_start), "toBlock": hex(cursor),
                    "topics": [TRANSFER_TOPIC, padded_addr, None],
                })
                calls_used += 1
                outbound.extend(_parse_transfer_logs(out_logs, decimals, "out"))
            except Exception as e:
                logger.warning("Outbound log fetch failed: %s", e)
                calls_used += 1

        cursor = chunk_start - 1

    inbound.sort(key=lambda x: x["timestamp"], reverse=True)
    outbound.sort(key=lambda x: x["timestamp"], reverse=True)
    return {"inbound": inbound[:limit], "outbound": outbound[:limit], "truncated": truncated}


def _parse_transfer_logs(logs: list, decimals: int, direction: str) -> list[dict]:
    entries = []
    for log in logs:
        try:
            block_num = int(log["blockNumber"], 16)
            raw_value = int(log.get("data", "0x0"), 16)
            topics = log.get("topics", [])

            entry = {
                "timestamp": f"block:{block_num}",
                "amount": str(raw_value / (10 ** decimals)),
                "txHash": log.get("transactionHash", ""),
            }

            if direction == "in":
                entry["from"] = unpad_address(topics[1]) if len(topics) > 1 else None
            else:
                entry["to"] = unpad_address(topics[2]) if len(topics) > 2 else None

            entries.append(entry)
        except Exception as e:
            logger.debug("Failed to parse transfer log: %s", e)
    return entries


# ============================================================
# Solana: Parallel Batch Parsing with Early Exit
# ============================================================


async def get_recent_transfers_solana(
    address: str, mint: str, decimals: int, limit: int = 5
) -> dict:
    budget = TRANSFER_BUDGET["solana"]
    start_time = time.monotonic()
    tx_parsed = 0

    token_accounts = await rpc.solana_get_token_accounts_by_owner(address, mint)
    if not token_accounts["value"]:
        return {**EMPTY_TRANSFERS}

    token_account = token_accounts["value"][0]["pubkey"]
    signatures = await rpc.solana_get_signatures_for_address(token_account, limit=budget["sig_fetch_limit"])

    inbound: list[dict] = []
    outbound: list[dict] = []
    truncated = False

    for batch_start in range(0, len(signatures), budget["parallel_batch_size"]):
        if (time.monotonic() - start_time) > budget["max_time_s"] or tx_parsed >= budget["max_tx_parsed"]:
            truncated = True
            break
        if len(inbound) >= budget["target_inbound"] and len(outbound) >= budget["target_outbound"]:
            break

        batch = signatures[batch_start : batch_start + budget["parallel_batch_size"]]
        results = await asyncio.gather(
            *[rpc.solana_get_transaction(sig["signature"]) for sig in batch],
            return_exceptions=True,
        )
        tx_parsed += len(batch)

        for tx in results:
            if isinstance(tx, Exception) or not tx or not tx.get("meta"):
                continue

            pre = _find_token_balance(tx["meta"].get("preTokenBalances", []), token_account, mint)
            post = _find_token_balance(tx["meta"].get("postTokenBalances", []), token_account, mint)
            diff = post - pre
            if diff == 0:
                continue

            block_time = tx.get("blockTime")
            entry = {
                "timestamp": datetime.fromtimestamp(block_time, tz=timezone.utc).isoformat() + "Z" if block_time else None,
                "amount": str(abs(diff) / (10 ** decimals)),
                "txHash": tx["transaction"]["signatures"][0],
            }

            if diff > 0:
                entry["from"] = _extract_counterparty(tx, "sender")
                inbound.append(entry)
            else:
                entry["to"] = _extract_counterparty(tx, "recipient")
                outbound.append(entry)

    return {"inbound": inbound[:limit], "outbound": outbound[:limit], "truncated": truncated}


def _find_token_balance(balances: list, token_account: str, mint: str) -> int:
    for b in balances:
        if b.get("mint") == mint:
            return int(b.get("uiTokenAmount", {}).get("amount", "0"))
    return 0


def _extract_counterparty(tx: dict, role: str) -> str | None:
    try:
        for ix in tx["transaction"]["message"]["instructions"]:
            parsed = ix.get("parsed")
            if isinstance(parsed, dict) and parsed.get("type") in ("transfer", "transferChecked"):
                info = parsed.get("info", {})
                return info.get("source" if role == "sender" else "destination") or info.get("authority")
    except Exception as e:
        logger.debug("Failed to extract counterparty: %s", e)
    return None


# ============================================================
# Dispatcher + Helpers
# ============================================================


async def get_recent_transfers(
    chain: str, address: str, token: str, decimals: int, limit: int = 5
) -> dict:
    if chain == "base":
        return await get_recent_transfers_base(address, token, decimals, limit)
    elif chain == "solana":
        return await get_recent_transfers_solana(address, token, decimals, limit)
    return {**EMPTY_TRANSFERS}


def derive_last_transfers(recent: dict) -> tuple[dict | None, dict | None]:
    return (
        recent["inbound"][0] if recent["inbound"] else None,
        recent["outbound"][0] if recent["outbound"] else None,
    )
