"""
Phase 4 Tests — Confidence Engine: Flags, flagScope, Notes
Tests all 11 flag conditions, flagScope metadata, and rule-based notes generator.
"""

import pytest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.services.confidence import detect_flags, build_flag_scope, generate_notes


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ============================================================
# Shared Test Data
# ============================================================

def _balance(formatted="1000.0", raw=1000):
    return {"formatted": formatted, "raw": raw, "decimals": 18}

def _token(symbol="DEGEN", address="0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed"):
    return {"symbol": symbol, "name": symbol, "address": address, "decimals": 18, "logo": None}

def _first_seen(ts=None, confidence="medium"):
    if ts is None:
        ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat() + "Z"
    return {"timestamp": ts, "confidence": confidence, "method": "test", "scanWindow": "90 days", "note": "test"}

def _transfers(inbound=None, outbound=None, truncated=False):
    return {"inbound": inbound or [], "outbound": outbound or [], "truncated": truncated}

def _inbound(n=1, from_addr="0x" + "a" * 40, amount="100.0"):
    return [{"timestamp": "2026-01-15T00:00:00Z", "amount": amount, "txHash": f"0xin{i}", "from": from_addr} for i in range(n)]

def _outbound(n=1, to_addr="0x" + "b" * 40, amount="50.0"):
    return [{"timestamp": "2026-01-10T00:00:00Z", "amount": amount, "txHash": f"0xout{i}", "to": to_addr} for i in range(n)]


# ============================================================
# Flag: zero_balance
# ============================================================

def test_flag_zero_balance():
    flags = detect_flags(_balance("0", 0), 0.0, _first_seen(), _transfers(), _token(), "base")
    assert flags == ["zero_balance"]

def test_flag_zero_balance_early_return():
    """zero_balance should be the ONLY flag returned."""
    flags = detect_flags(_balance("0", 0), 0.0, _first_seen(), _transfers(inbound=_inbound(5)), _token(), "base")
    assert flags == ["zero_balance"]


# ============================================================
# Flag: dust_amount
# ============================================================

def test_flag_dust_amount():
    flags = detect_flags(_balance("0.001"), 0.50, _first_seen(), _transfers(), _token(), "base")
    assert "dust_amount" in flags

def test_flag_no_dust_above_threshold():
    flags = detect_flags(_balance("100.0"), 5.0, _first_seen(), _transfers(), _token(), "base")
    assert "dust_amount" not in flags


# ============================================================
# Flag: large_holder
# ============================================================

def test_flag_large_holder():
    flags = detect_flags(_balance("1000000.0"), 15000.0, _first_seen(), _transfers(), _token(), "base")
    assert "large_holder" in flags

def test_flag_no_large_holder_below_threshold():
    flags = detect_flags(_balance("100.0"), 500.0, _first_seen(), _transfers(), _token(), "base")
    assert "large_holder" not in flags


# ============================================================
# Flag: recently_acquired
# ============================================================

def test_flag_recently_acquired():
    ts = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat() + "Z"
    flags = detect_flags(_balance(), 100.0, _first_seen(ts, "medium"), _transfers(), _token(), "base")
    assert "recently_acquired" in flags

def test_flag_not_recently_acquired_old():
    ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat() + "Z"
    flags = detect_flags(_balance(), 100.0, _first_seen(ts, "medium"), _transfers(), _token(), "base")
    assert "recently_acquired" not in flags

def test_flag_not_recently_acquired_low_confidence():
    """recently_acquired requires medium/high confidence."""
    ts = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat() + "Z"
    flags = detect_flags(_balance(), 100.0, _first_seen(ts, "low"), _transfers(), _token(), "base")
    assert "recently_acquired" not in flags


# ============================================================
# Flag: single_transfer_in
# ============================================================

def test_flag_single_transfer_in():
    flags = detect_flags(_balance(), 100.0, _first_seen(), _transfers(inbound=_inbound(1)), _token(), "base")
    assert "single_transfer_in" in flags

def test_flag_no_single_transfer_with_outbound():
    """single_transfer_in requires exactly 1 inbound AND 0 outbound."""
    flags = detect_flags(_balance(), 100.0, _first_seen(), _transfers(inbound=_inbound(1), outbound=_outbound(1)), _token(), "base")
    assert "single_transfer_in" not in flags


# ============================================================
# Flag: multiple_inflows
# ============================================================

def test_flag_multiple_inflows():
    flags = detect_flags(_balance(), 100.0, _first_seen(), _transfers(inbound=_inbound(3)), _token(), "base")
    assert "multiple_inflows" in flags

def test_flag_no_multiple_inflows_below_3():
    flags = detect_flags(_balance(), 100.0, _first_seen(), _transfers(inbound=_inbound(2)), _token(), "base")
    assert "multiple_inflows" not in flags


# ============================================================
# Flag: frequent_trader
# ============================================================

def test_flag_frequent_trader():
    flags = detect_flags(_balance(), 100.0, _first_seen(), _transfers(inbound=_inbound(5), outbound=_outbound(5)), _token(), "base")
    assert "frequent_trader" in flags

def test_flag_no_frequent_trader_below_10():
    flags = detect_flags(_balance(), 100.0, _first_seen(), _transfers(inbound=_inbound(3), outbound=_outbound(3)), _token(), "base")
    assert "frequent_trader" not in flags


# ============================================================
# Flag: dex_router_source
# ============================================================

def test_flag_dex_router_source():
    """Inbound from known Uniswap router → dex_router_source."""
    router = "0x2626664c2603336e57b271c5c0b26f421741e481"
    transfers = _transfers(inbound=_inbound(1, from_addr=router))
    flags = detect_flags(_balance(), 100.0, _first_seen(), transfers, _token(), "base")
    assert "dex_router_source" in flags

def test_flag_no_dex_router_unknown_source():
    transfers = _transfers(inbound=_inbound(1, from_addr="0x" + "f" * 40))
    flags = detect_flags(_balance(), 100.0, _first_seen(), transfers, _token(), "base")
    assert "dex_router_source" not in flags


# ============================================================
# Flag: possible_airdrop
# ============================================================

def test_flag_possible_airdrop():
    distributor = "0x777777c338d5487fdecc5b15949cc8e9f69a7899"
    transfers = _transfers(inbound=_inbound(1, from_addr=distributor))
    flags = detect_flags(_balance(), 100.0, _first_seen(), transfers, _token(), "base")
    assert "possible_airdrop" in flags

def test_flag_no_airdrop_multiple_inbound():
    """possible_airdrop requires exactly 1 inbound."""
    distributor = "0x777777c338d5487fdecc5b15949cc8e9f69a7899"
    transfers = _transfers(inbound=_inbound(2, from_addr=distributor))
    flags = detect_flags(_balance(), 100.0, _first_seen(), transfers, _token(), "base")
    assert "possible_airdrop" not in flags


# ============================================================
# Flag: wrapped_token
# ============================================================

def test_flag_wrapped_token_base():
    token = _token(symbol="WETH", address="0x4200000000000000000000000000000000000006")
    flags = detect_flags(_balance(), 100.0, _first_seen(), _transfers(), token, "base")
    assert "wrapped_token" in flags

def test_flag_wrapped_token_solana():
    token = _token(symbol="wSOL", address="So11111111111111111111111111111111111111112")
    flags = detect_flags(_balance(), 100.0, _first_seen(), _transfers(), token, "solana")
    assert "wrapped_token" in flags


# ============================================================
# Flag: lp_token
# ============================================================

def test_flag_lp_token():
    token = _token(symbol="UNI-V2", address="0x" + "c" * 40)
    flags = detect_flags(_balance(), 100.0, _first_seen(), _transfers(), token, "base")
    assert "lp_token" in flags

def test_flag_no_lp_regular_token():
    flags = detect_flags(_balance(), 100.0, _first_seen(), _transfers(), _token(), "base")
    assert "lp_token" not in flags


# ============================================================
# flagScope
# ============================================================

def test_flag_scope_base():
    scope = build_flag_scope("base", "standard", {"blocks_scanned": 3888000})
    assert scope["type"] == "block_window"
    assert scope["approxDays"] == 90
    assert scope["depth"] == "standard"
    assert scope["blocksScanned"] == 3888000

def test_flag_scope_solana():
    scope = build_flag_scope("solana", "deep", {"sigs_scanned": 800, "tx_parsed": 15})
    assert scope["type"] == "signature_window"
    assert scope["depth"] == "deep"
    assert scope["signaturesScanned"] == 800
    assert scope["txParsed"] == 15

def test_flag_scope_unknown_chain():
    scope = build_flag_scope("polygon", "standard", {})
    assert scope["type"] == "unknown"


# ============================================================
# Notes Generator
# ============================================================

def test_notes_zero_balance():
    notes = generate_notes(["zero_balance"], _first_seen(), _transfers(), _balance("0"))
    assert len(notes) == 1
    assert "zero" in notes[0].lower()

def test_notes_multiple_inflows():
    notes = generate_notes(["multiple_inflows"], _first_seen(), _transfers(inbound=_inbound(3)), _balance())
    assert any("multiple transactions" in n for n in notes)

def test_notes_single_transfer():
    notes = generate_notes(["single_transfer_in"], _first_seen(), _transfers(inbound=_inbound(1)), _balance())
    assert any("single transaction" in n for n in notes)

def test_notes_recently_acquired():
    ts = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat() + "Z"
    notes = generate_notes(["recently_acquired"], _first_seen(ts), _transfers(), _balance())
    assert any("days ago" in n for n in notes)

def test_notes_low_confidence():
    fs = _first_seen(confidence="low")
    notes = generate_notes([], fs, _transfers(), _balance())
    assert any("low confidence" in n for n in notes)

def test_notes_net_outflow():
    """Outbound transfers + positive balance → net flow note."""
    notes = generate_notes(
        [],
        _first_seen(),
        _transfers(inbound=_inbound(1, amount="200.0"), outbound=_outbound(1, amount="50.0")),
        _balance("150.0"),
    )
    assert any("transferred out" in n for n in notes)

def test_notes_truncated():
    notes = generate_notes([], _first_seen(), _transfers(truncated=True), _balance())
    assert any("truncated" in n for n in notes)

def test_notes_lp_token():
    notes = generate_notes(["lp_token"], _first_seen(), _transfers(), _balance())
    assert any("liquidity pool" in n for n in notes)

def test_notes_wrapped_token():
    notes = generate_notes(["wrapped_token"], _first_seen(), _transfers(), _balance())
    assert any("wrapped" in n for n in notes)

def test_notes_dex_source():
    notes = generate_notes(["dex_router_source"], _first_seen(), _transfers(), _balance())
    assert any("DEX swap" in n for n in notes)

def test_notes_airdrop():
    notes = generate_notes(["possible_airdrop"], _first_seen(), _transfers(), _balance())
    assert any("airdrop" in n for n in notes)

def test_notes_frequent_trader():
    notes = generate_notes(["frequent_trader"], _first_seen(), _transfers(), _balance())
    assert any("actively trades" in n for n in notes)


# ============================================================
# Integration: Full Response Shape
# ============================================================

MOCK_FIRST_SEEN = {
    "timestamp": "2025-11-20T14:30:00Z",
    "confidence": "medium",
    "method": "chunked_log_scan",
    "scanWindow": "90 days",
    "note": "test",
}
MOCK_BALANCE = {"raw": 42069500000000000000000, "decimals": 18, "formatted": "42069.5"}
MOCK_META = {
    "address": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
    "symbol": "DEGEN",
    "name": "Degen",
    "decimals": 18,
    "logo": None,
}
MOCK_TRANSFERS = {
    "inbound": [
        {"timestamp": "2026-01-15T09:00:00Z", "amount": "10000.0", "txHash": "0xdef456", "from": "0x" + "a" * 40},
        {"timestamp": "2025-12-20T11:00:00Z", "amount": "20000.0", "txHash": "0xghi789", "from": "0x" + "b" * 40},
        {"timestamp": "2025-12-01T08:00:00Z", "amount": "15000.0", "txHash": "0xjkl012", "from": "0x" + "c" * 40},
    ],
    "outbound": [
        {"timestamp": "2025-12-01T12:00:00Z", "amount": "5000.0", "txHash": "0xmno345", "to": "0x" + "d" * 40},
    ],
    "truncated": False,
}


@pytest.mark.anyio
@patch("app.routes.position_receipt.get_recent_transfers")
@patch("app.routes.position_receipt.estimate_first_seen")
@patch("app.routes.position_receipt.get_token_balance")
@patch("app.routes.position_receipt.resolve_token")
@patch("app.routes.position_receipt.get_token_price_cached")
async def test_full_response_shape(mock_price, mock_meta, mock_balance, mock_first_seen, mock_transfers, client):
    """Full integration test — verify all Phase 1-4 fields present."""
    mock_balance.return_value = MOCK_BALANCE
    mock_meta.return_value = MOCK_META
    mock_price.return_value = 0.0297
    mock_first_seen.return_value = MOCK_FIRST_SEEN
    mock_transfers.return_value = MOCK_TRANSFERS

    resp = await client.post(
        "/v1/position-receipt/base",
        json={
            "address": "0x1234567890abcdef1234567890abcdef12345678",
            "token": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
            "depth": "standard",
        },
    )
    assert resp.status_code == 200
    data = resp.json()

    # Phase 1
    assert data["address"] == "0x1234567890abcdef1234567890abcdef12345678"
    assert data["chain"] == "base"
    assert data["token"]["symbol"] == "DEGEN"
    assert data["currentBalance"] == "42069.5"
    assert data["pricePerToken"] == 0.0297
    assert data["currentValueUsd"] is not None

    # Phase 2
    assert data["firstSeenApprox"]["confidence"] == "medium"
    assert data["holdingDurationDays"] is not None

    # Phase 3
    assert data["lastTransferIn"] is not None
    assert data["lastTransferIn"]["txHash"] == "0xdef456"
    assert data["lastTransferOut"] is not None
    assert data["lastTransferOut"]["txHash"] == "0xmno345"
    assert len(data["recentTransfers"]["inbound"]) == 3
    assert len(data["recentTransfers"]["outbound"]) == 1
    assert data["recentTransfers"]["truncated"] is False

    # Phase 4
    assert "multiple_inflows" in data["flags"]
    assert data["flagScope"]["type"] == "block_window"
    assert data["flagScope"]["depth"] == "standard"
    assert len(data["notes"]) > 0
    assert any("multiple transactions" in n for n in data["notes"])
    assert any("transferred out" in n for n in data["notes"])

    # Card (not yet implemented)
    assert data["card"] is None
