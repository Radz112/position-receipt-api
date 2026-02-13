"""
Phase 2 Tests — First-Seen Estimator (Bounded Scans)
Tests chunked log scan (Base), multi-account signature scan (Solana),
cap enforcement, confidence levels, boundary detection, and degradation.
"""

import time
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.services.first_seen import (
    estimate_first_seen_base,
    estimate_first_seen_solana,
    estimate_first_seen,
)
from app.utils.evm import pad_address, TRANSFER_TOPIC


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ============================================================
# Unit Tests — Helpers
# ============================================================


def test_pad_address():
    addr = "0x1234567890abcdef1234567890abcdef12345678"
    padded = pad_address(addr)
    assert padded.startswith("0x")
    assert len(padded) == 66  # 0x + 64 hex chars
    assert padded.endswith("1234567890abcdef1234567890abcdef12345678")
    # Should be zero-padded on the left
    assert padded == "0x0000000000000000000000001234567890abcdef1234567890abcdef12345678"


def test_transfer_topic():
    # keccak256("Transfer(address,address,uint256)")
    assert TRANSFER_TOPIC == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


# ============================================================
# Base: Chunked Log Scan — Happy Path
# ============================================================


@pytest.mark.anyio
@patch("app.services.first_seen.rpc")
async def test_base_first_seen_found(mock_rpc):
    """Find a Transfer event in the scan window → medium confidence."""
    current_block = 20_000_000
    now = int(time.time())
    mock_rpc.eth_block_number = AsyncMock(return_value=current_block)

    # scan_start_block ≈ 20M - (90*86400/2) ≈ 16,112,000
    # Place hit well inside the window (not near boundary)
    hit_block = current_block - 1_000_000  # 19M — well within window

    # eth_get_block_by_number is called twice:
    #   1. _timestamp_to_block → needs current block's real timestamp
    #   2. _get_block_timestamp → returns the hit block's timestamp (60 days ago)
    mock_rpc.eth_get_block_by_number = AsyncMock(side_effect=[
        {"timestamp": hex(now)},                    # _timestamp_to_block anchor
        {"timestamp": hex(now - 60 * 86400)},       # _get_block_timestamp for hit
    ])

    mock_rpc.eth_get_logs = AsyncMock(side_effect=[
        [],  # First chunk — no hits
        [{"blockNumber": hex(hit_block), "topics": ["0x...", "0x...", "0x..."]}],  # Second chunk — hit!
    ])

    result = await estimate_first_seen_base(
        "0x1234567890abcdef1234567890abcdef12345678",
        "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
        "standard",
    )

    assert result["confidence"] == "medium"
    assert result["method"] == "chunked_log_scan"
    assert result["timestamp"] is not None
    assert result["scanWindow"] == "90 days"


# ============================================================
# Base: No Transfer Events Found
# ============================================================


@pytest.mark.anyio
@patch("app.services.first_seen.rpc")
async def test_base_first_seen_not_found(mock_rpc):
    """No Transfer events in window → low confidence, null timestamp."""
    current_block = 20_000_000
    mock_rpc.eth_block_number = AsyncMock(return_value=current_block)
    mock_rpc.eth_get_block_by_number = AsyncMock(return_value={
        "timestamp": hex(int(time.time())),
    })
    # All chunks return empty
    mock_rpc.eth_get_logs = AsyncMock(return_value=[])

    result = await estimate_first_seen_base(
        "0x1234567890abcdef1234567890abcdef12345678",
        "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
        "fast",
    )

    assert result["confidence"] == "low"
    assert result["timestamp"] is None
    assert "No Transfer events found" in result["note"]


# ============================================================
# Base: RPC Call Cap Enforcement
# ============================================================


@pytest.mark.anyio
@patch("app.services.first_seen.rpc")
async def test_base_rpc_cap_enforcement(mock_rpc):
    """Should stop scanning when max_rpc_calls is reached."""
    current_block = 20_000_000
    mock_rpc.eth_block_number = AsyncMock(return_value=current_block)
    mock_rpc.eth_get_block_by_number = AsyncMock(return_value={
        "timestamp": hex(int(time.time())),
    })
    # All chunks return empty — will exhaust call budget
    mock_rpc.eth_get_logs = AsyncMock(return_value=[])

    result = await estimate_first_seen_base(
        "0x1234567890abcdef1234567890abcdef12345678",
        "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
        "fast",  # max_rpc_calls = 8
    )

    # eth_block_number (1) + eth_get_block_by_number (1) + eth_getLogs calls
    # Total should not exceed 8
    total_calls = (
        mock_rpc.eth_block_number.call_count
        + mock_rpc.eth_get_block_by_number.call_count
        + mock_rpc.eth_get_logs.call_count
    )
    assert total_calls <= 8
    assert result["confidence"] == "low"


# ============================================================
# Base: Boundary Detection — Near Window Edge
# ============================================================


@pytest.mark.anyio
@patch("app.services.first_seen.rpc")
async def test_base_boundary_detection(mock_rpc):
    """Hit found near scan window boundary → low confidence."""
    current_block = 20_000_000
    current_ts = int(time.time())
    mock_rpc.eth_block_number = AsyncMock(return_value=current_block)
    mock_rpc.eth_get_block_by_number = AsyncMock(return_value={
        "timestamp": hex(current_ts),
    })

    # Calculate scan_start_block roughly (90 days / 2s = ~3,888,000 blocks)
    scan_start_approx = current_block - int(90 * 86400 / 2.0)
    # Hit block is very close to scan start (within 1000 blocks)
    hit_block = scan_start_approx + 500

    mock_rpc.eth_get_logs = AsyncMock(side_effect=[
        [{"blockNumber": hex(hit_block), "topics": ["0x...", "0x...", "0x..."]}],
    ])

    result = await estimate_first_seen_base(
        "0x1234567890abcdef1234567890abcdef12345678",
        "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
        "standard",
    )

    assert result["confidence"] == "low"
    assert "boundary" in result["note"]


# ============================================================
# Base: Depth Affects Window Size
# ============================================================


@pytest.mark.anyio
@patch("app.services.first_seen.rpc")
async def test_base_depth_affects_window(mock_rpc):
    """Different depths produce different scan windows."""
    current_block = 20_000_000
    mock_rpc.eth_block_number = AsyncMock(return_value=current_block)
    mock_rpc.eth_get_block_by_number = AsyncMock(return_value={
        "timestamp": hex(int(time.time())),
    })
    mock_rpc.eth_get_logs = AsyncMock(return_value=[])

    fast = await estimate_first_seen_base("0x" + "1" * 40, "0x" + "2" * 40, "fast")
    assert fast["scanWindow"] == "30 days"

    mock_rpc.eth_block_number.reset_mock()
    mock_rpc.eth_get_block_by_number.reset_mock()
    mock_rpc.eth_get_logs.reset_mock()

    deep = await estimate_first_seen_base("0x" + "1" * 40, "0x" + "2" * 40, "deep")
    assert deep["scanWindow"] == "180 days"


# ============================================================
# Solana: Happy Path — Full History Scanned
# ============================================================


@pytest.mark.anyio
@patch("app.services.first_seen.rpc")
async def test_solana_first_seen_full_history(mock_rpc):
    """Full history scanned, < sig limit → high confidence."""
    mock_rpc.solana_get_token_accounts_by_owner = AsyncMock(return_value={
        "value": [{"pubkey": "TokenAccount111111111111111111111111111111"}],
    })

    oldest_time = int(time.time()) - 120 * 86400  # 120 days ago
    mock_rpc.solana_get_signatures_for_address = AsyncMock(return_value=[
        {"signature": "sig1", "blockTime": int(time.time()) - 10 * 86400},
        {"signature": "sig2", "blockTime": int(time.time()) - 30 * 86400},
        {"signature": "sig3", "blockTime": oldest_time},
    ])

    result = await estimate_first_seen_solana(
        "DYw8jCTfwHNRJhhmFcbXvVDTqWMEVFBX6ZKUmG5CNSKK",
        "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
        "standard",
    )

    assert result["confidence"] == "high"
    assert result["timestamp"] is not None
    assert "Full history scanned" in result["note"]
    assert result["method"] == "token_account_scan"


# ============================================================
# Solana: No Token Account Found
# ============================================================


@pytest.mark.anyio
@patch("app.services.first_seen.rpc")
async def test_solana_no_token_account(mock_rpc):
    """No token account for this mint → low confidence."""
    mock_rpc.solana_get_token_accounts_by_owner = AsyncMock(return_value={
        "value": [],
    })

    result = await estimate_first_seen_solana(
        "DYw8jCTfwHNRJhhmFcbXvVDTqWMEVFBX6ZKUmG5CNSKK",
        "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
        "standard",
    )

    assert result["confidence"] == "low"
    assert result["timestamp"] is None
    assert "No token account" in result["note"]


# ============================================================
# Solana: Signature Limit Hit
# ============================================================


@pytest.mark.anyio
@patch("app.services.first_seen.rpc")
async def test_solana_sig_limit_hit(mock_rpc):
    """Signature limit reached → low confidence (more history likely exists)."""
    mock_rpc.solana_get_token_accounts_by_owner = AsyncMock(return_value={
        "value": [{"pubkey": "TokenAccount111111111111111111111111111111"}],
    })

    # Return exactly the limit (200 for fast) → indicates more history exists
    oldest_time = int(time.time()) - 30 * 86400
    sigs = [
        {"signature": f"sig{i}", "blockTime": int(time.time()) - i * 1000}
        for i in range(200)
    ]
    sigs[-1]["blockTime"] = oldest_time  # Oldest

    mock_rpc.solana_get_signatures_for_address = AsyncMock(return_value=sigs)

    result = await estimate_first_seen_solana(
        "DYw8jCTfwHNRJhhmFcbXvVDTqWMEVFBX6ZKUmG5CNSKK",
        "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
        "fast",  # sol_sigs = 200
    )

    assert result["confidence"] == "low"
    assert result["timestamp"] is not None
    assert "Scan limit reached" in result["note"]


# ============================================================
# Solana: Multiple Token Accounts
# ============================================================


@pytest.mark.anyio
@patch("app.services.first_seen.rpc")
async def test_solana_multiple_accounts(mock_rpc):
    """Scans all token accounts, finds oldest across them."""
    mock_rpc.solana_get_token_accounts_by_owner = AsyncMock(return_value={
        "value": [
            {"pubkey": "TokenAccount1111111111111111111111111111111"},
            {"pubkey": "TokenAccount2222222222222222222222222222222"},
        ],
    })

    older_time = int(time.time()) - 200 * 86400  # 200 days ago
    newer_time = int(time.time()) - 10 * 86400    # 10 days ago

    async def mock_sigs(address, limit=1000):
        if "1111" in address:
            return [{"signature": "sig_new", "blockTime": newer_time}]
        else:
            return [{"signature": "sig_old", "blockTime": older_time}]

    mock_rpc.solana_get_signatures_for_address = AsyncMock(side_effect=mock_sigs)

    result = await estimate_first_seen_solana(
        "DYw8jCTfwHNRJhhmFcbXvVDTqWMEVFBX6ZKUmG5CNSKK",
        "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
        "standard",
    )

    assert result["confidence"] == "high"
    # Should pick the OLDER timestamp from account 2
    from datetime import datetime, timezone
    result_ts = datetime.fromisoformat(result["timestamp"].rstrip("Z")).replace(tzinfo=timezone.utc)
    expected_ts = datetime.fromtimestamp(older_time, tz=timezone.utc)
    assert abs((result_ts - expected_ts).total_seconds()) < 2
    assert "2 token account" in result["note"]


# ============================================================
# Dispatcher
# ============================================================


@pytest.mark.anyio
@patch("app.services.first_seen.estimate_first_seen_base")
async def test_dispatcher_base(mock_base):
    mock_base.return_value = {"timestamp": None, "confidence": "low", "method": "chunked_log_scan", "scanWindow": "90 days", "note": "test"}
    result = await estimate_first_seen("base", "0x" + "1" * 40, "0x" + "2" * 40, "standard")
    assert result["method"] == "chunked_log_scan"
    mock_base.assert_called_once()


@pytest.mark.anyio
@patch("app.services.first_seen.estimate_first_seen_solana")
async def test_dispatcher_solana(mock_sol):
    mock_sol.return_value = {"timestamp": None, "confidence": "low", "method": "token_account_scan", "scanWindow": "0", "note": "test"}
    result = await estimate_first_seen("solana", "DYw8jCTfwHNRJhhmFcbXvVDTqWMEVFBX6ZKUmG5CNSKK", "mint123", "standard")
    assert result["method"] == "token_account_scan"
    mock_sol.assert_called_once()


@pytest.mark.anyio
async def test_dispatcher_unsupported_chain():
    result = await estimate_first_seen("polygon", "0x" + "1" * 40, "0x" + "2" * 40)
    assert result["confidence"] == "low"
    assert "Unsupported" in result["note"]


# ============================================================
# holdingDurationDays — Integration with Route
# ============================================================


MOCK_FIRST_SEEN = {
    "timestamp": "2025-11-20T14:30:00Z",
    "confidence": "medium",
    "method": "chunked_log_scan",
    "scanWindow": "90 days",
    "note": "Based on first Transfer event within 90-day window",
}

MOCK_FIRST_SEEN_LOW = {
    "timestamp": "2025-11-20T14:30:00Z",
    "confidence": "low",
    "method": "chunked_log_scan",
    "scanWindow": "90 days",
    "note": "Scan budget exhausted",
}

MOCK_BALANCE = {"raw": 1000, "decimals": 18, "formatted": "0.000000000000001"}
MOCK_META = {
    "address": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
    "symbol": "DEGEN",
    "name": "Degen",
    "decimals": 18,
    "logo": None,
}


@pytest.mark.anyio
@patch("app.routes.position_receipt.estimate_first_seen")
@patch("app.routes.position_receipt.get_token_balance")
@patch("app.routes.position_receipt.resolve_token")
@patch("app.routes.position_receipt.get_token_price_cached")
async def test_holding_duration_medium_confidence(mock_price, mock_meta, mock_balance, mock_first_seen, client):
    """holdingDurationDays computed when confidence is medium."""
    mock_balance.return_value = MOCK_BALANCE
    mock_meta.return_value = MOCK_META
    mock_price.return_value = 0.03
    mock_first_seen.return_value = MOCK_FIRST_SEEN

    resp = await client.post(
        "/v1/position-receipt/base",
        json={
            "address": "0x1234567890abcdef1234567890abcdef12345678",
            "token": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
        },
    )
    data = resp.json()
    assert resp.status_code == 200
    assert data["holdingDurationDays"] is not None
    assert data["holdingDurationDays"] > 0
    assert data["firstSeenApprox"]["confidence"] == "medium"


@pytest.mark.anyio
@patch("app.routes.position_receipt.estimate_first_seen")
@patch("app.routes.position_receipt.get_token_balance")
@patch("app.routes.position_receipt.resolve_token")
@patch("app.routes.position_receipt.get_token_price_cached")
async def test_holding_duration_low_confidence_null(mock_price, mock_meta, mock_balance, mock_first_seen, client):
    """holdingDurationDays is null when confidence is low."""
    mock_balance.return_value = MOCK_BALANCE
    mock_meta.return_value = MOCK_META
    mock_price.return_value = 0.03
    mock_first_seen.return_value = MOCK_FIRST_SEEN_LOW

    resp = await client.post(
        "/v1/position-receipt/base",
        json={
            "address": "0x1234567890abcdef1234567890abcdef12345678",
            "token": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
        },
    )
    data = resp.json()
    assert resp.status_code == 200
    assert data["holdingDurationDays"] is None
    assert data["firstSeenApprox"]["confidence"] == "low"


@pytest.mark.anyio
@patch("app.routes.position_receipt.estimate_first_seen")
@patch("app.routes.position_receipt.get_token_balance")
@patch("app.routes.position_receipt.resolve_token")
@patch("app.routes.position_receipt.get_token_price_cached")
async def test_holding_duration_no_timestamp(mock_price, mock_meta, mock_balance, mock_first_seen, client):
    """holdingDurationDays is null when timestamp is null."""
    mock_balance.return_value = MOCK_BALANCE
    mock_meta.return_value = MOCK_META
    mock_price.return_value = 0.03
    mock_first_seen.return_value = {
        "timestamp": None,
        "confidence": "low",
        "method": "chunked_log_scan",
        "scanWindow": "90 days",
        "note": "No events found",
    }

    resp = await client.post(
        "/v1/position-receipt/base",
        json={
            "address": "0x1234567890abcdef1234567890abcdef12345678",
            "token": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
        },
    )
    data = resp.json()
    assert data["holdingDurationDays"] is None


@pytest.mark.anyio
@patch("app.routes.position_receipt.estimate_first_seen")
@patch("app.routes.position_receipt.get_token_balance")
@patch("app.routes.position_receipt.resolve_token")
@patch("app.routes.position_receipt.get_token_price_cached")
async def test_first_seen_error_graceful(mock_price, mock_meta, mock_balance, mock_first_seen, client):
    """First-seen estimation failure degrades gracefully."""
    mock_balance.return_value = MOCK_BALANCE
    mock_meta.return_value = MOCK_META
    mock_price.return_value = 0.03
    mock_first_seen.side_effect = Exception("RPC exploded")

    resp = await client.post(
        "/v1/position-receipt/base",
        json={
            "address": "0x1234567890abcdef1234567890abcdef12345678",
            "token": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
        },
    )
    assert resp.status_code == 200  # Not a 502 — first-seen is non-fatal
    data = resp.json()
    assert data["firstSeenApprox"]["confidence"] == "low"
    assert data["firstSeenApprox"]["method"] == "error"
    assert data["holdingDurationDays"] is None


# ============================================================
# Base: eth_getLogs Error Handling
# ============================================================


@pytest.mark.anyio
@patch("app.services.first_seen.rpc")
async def test_base_log_error_continues(mock_rpc):
    """If a chunk's eth_getLogs fails, scanning continues to next chunk."""
    current_block = 20_000_000
    mock_rpc.eth_block_number = AsyncMock(return_value=current_block)
    mock_rpc.eth_get_block_by_number = AsyncMock(return_value={
        "timestamp": hex(int(time.time())),
    })

    hit_block = current_block - 1_000_000
    mock_rpc.eth_get_logs = AsyncMock(side_effect=[
        Exception("RPC rate limit"),  # First chunk fails
        [{"blockNumber": hex(hit_block), "topics": ["0x...", "0x...", "0x..."]}],  # Second chunk succeeds
    ])

    result = await estimate_first_seen_base(
        "0x1234567890abcdef1234567890abcdef12345678",
        "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
        "standard",
    )

    # Should still find the hit despite the first chunk error
    assert result["timestamp"] is not None
