import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Request

from app.config import NATIVE_TOKENS
from app.utils.params import extract_param
from app.utils.errors import error_response
from app.utils.validation import validate_chain, validate_address, validate_token, validate_depth
from app.services.balance import get_token_balance
from app.services.token_metadata import resolve_token, resolve_symbol_to_address
from app.services.price import get_token_price_cached
from app.services.first_seen import estimate_first_seen
from app.services.transfers import get_recent_transfers, derive_last_transfers
from app.services.confidence import detect_flags, build_flag_scope, generate_notes, parse_iso

logger = logging.getLogger("apix")

router = APIRouter(prefix="/v1/position-receipt")

_EMPTY_FIRST_SEEN = {"timestamp": None, "confidence": "low", "scanWindow": "0"}
_EMPTY_TRANSFERS = {"inbound": [], "outbound": [], "truncated": False}


@router.get("/{chain}")
async def position_receipt_info(chain: str):
    err = validate_chain(chain)
    if err:
        return error_response(400, "invalid_chain", err, None)

    return {
        "endpoint": f"/v1/position-receipt/{chain}",
        "method": "POST",
        "description": (
            f"Verify a wallet's current position in any token on {chain.title()} "
            "— single-token verification with holding duration estimates and confidence levels"
        ),
        "chain": chain,
        "parameters": {
            "address": "Wallet address (required)",
            "token": "Token contract address or mint address (required)",
            "depth": "fast | standard | deep (optional, default: standard) — controls scan window size",
            "generateCard": "true | false (optional, default: false)",
            "cardTemplate": "classic | minimal | dark (optional, default: classic)",
        },
        "pricing": "x402 micropayment per request",
    }


@router.post("/{chain}")
async def position_receipt(chain: str, request: Request):
    # --- Parse body ---
    try:
        body = await request.json()
    except Exception:
        return error_response(400, "invalid_body", "Request body must be valid JSON", None)

    if not isinstance(body, dict):
        return error_response(400, "invalid_body", "Request body must be a JSON object", {"raw": str(body)[:200]})

    # --- Extract & validate ---
    address = extract_param(body, "address", aliases=["wallet", "addr"], use_query_fallback=True)
    token = extract_param(body, "token", aliases=["mint", "contract", "token_address"])
    depth = extract_param(body, "depth") or "standard"

    # If token looks like a ticker symbol (not an address), try to resolve it
    if token and isinstance(token, str) and validate_token(chain, token) is not None and token.lower() not in ("eth", "sol"):
        resolved = await resolve_symbol_to_address(chain, token)
        if resolved:
            token = resolved
        else:
            return error_response(400, "unknown_symbol", f"Could not resolve token symbol '{token}' to an address on {chain}", body)

    for check, code, msg in [
        (validate_chain(chain), "invalid_chain", None),
        (None if address and isinstance(address, str) else "required", "missing_address", "address is required"),
        (None if token and isinstance(token, str) else "required", "missing_token", "token is required"),
    ]:
        if check:
            return error_response(400, code, msg or check, body)

    err = validate_address(chain, address)
    if err:
        return error_response(400, "invalid_address", err, body)
    err = validate_token(chain, token)
    if err:
        return error_response(400, "invalid_token", err, body)
    if isinstance(depth, str):
        err = validate_depth(depth)
        if err:
            return error_response(400, "invalid_depth", err, body)

    _native_check = token.lower() if chain != "solana" else token
    is_native = _native_check in NATIVE_TOKENS.get(chain, set()) or token.lower() in ("eth", "sol")

    # --- Concurrent fetch: balance + metadata + price ---
    try:
        balance_result, token_meta, price = await asyncio.gather(
            get_token_balance(chain, address, token),
            resolve_token(chain, token),
            get_token_price_cached(chain, token),
        )
    except Exception as e:
        logger.error("Fetch error: %s", e, exc_info=True)
        err_msg = str(e).lower()
        if "not found" in err_msg or "account not found" in err_msg:
            return error_response(404, "token_not_found", f"Token not found on {chain}: {token}", body)
        return error_response(502, "upstream_error", f"Failed to fetch data: {e}", body)

    # --- First-seen estimation ---
    if is_native:
        first_seen = {**_EMPTY_FIRST_SEEN, "method": "skipped", "note": "First-seen estimation not available for native tokens"}
    else:
        try:
            first_seen = await estimate_first_seen(chain, address, token, depth)
        except Exception as e:
            logger.warning("First-seen estimation failed: %s", e)
            first_seen = {**_EMPTY_FIRST_SEEN, "method": "error", "note": f"First-seen estimation failed: {e}"}

    # Holding duration (medium/high confidence only)
    holding_duration_days = None
    if first_seen.get("timestamp") and first_seen.get("confidence") in ("medium", "high"):
        holding_duration_days = (datetime.now(timezone.utc) - parse_iso(first_seen["timestamp"])).days

    # --- Recent transfers ---
    if is_native:
        recent_transfers = {**_EMPTY_TRANSFERS}
    else:
        try:
            recent_transfers = await get_recent_transfers(chain, address, token, token_meta.get("decimals", 18))
        except Exception as e:
            logger.warning("Recent transfers fetch failed: %s", e)
            recent_transfers = {**_EMPTY_TRANSFERS}

    last_in, last_out = derive_last_transfers(recent_transfers)

    # --- Compute values ---
    current_balance = balance_result["formatted"]
    current_value_usd = round(float(Decimal(current_balance) * Decimal(str(price))), 2) if price is not None and current_balance != "0" else None

    flags = detect_flags(balance_result, current_value_usd, first_seen, recent_transfers, token_meta, chain)

    return {
        "address": address,
        "chain": chain,
        "token": {
            "address": token_meta.get("address", token),
            "symbol": token_meta.get("symbol"),
            "name": token_meta.get("name"),
            "decimals": token_meta.get("decimals"),
            "logo": token_meta.get("logo"),
        },
        "currentBalance": current_balance,
        "currentValueUsd": current_value_usd,
        "pricePerToken": price,
        "firstSeenApprox": first_seen,
        "holdingDurationDays": holding_duration_days,
        "lastTransferIn": last_in,
        "lastTransferOut": last_out,
        "recentTransfers": recent_transfers,
        "flags": flags,
        "flagScope": build_flag_scope(chain, depth, {"blocks_scanned": 0, "sigs_scanned": 0, "tx_parsed": 0}),
        "notes": generate_notes(flags, first_seen, recent_transfers, balance_result),
        "card": None,
    }
