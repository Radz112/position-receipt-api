"""
Phase 5 Tests — Rate Limiting, Edge Cases, Error Responses
Tests in-memory rate limiter, native token handling, token-not-found,
upstream error propagation, and error response shape.
"""

import time
import pytest
from unittest.mock import AsyncMock, patch, PropertyMock
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.middleware.rate_limit import (
    _hits, _is_limited, _record, _prune, reset_rate_limits,
)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ============================================================
# Rate Limiter — Unit Tests
# ============================================================


def test_rate_limiter_prune():
    """Expired entries are pruned from the sliding window."""
    now = time.monotonic()
    # Add entries: 3 old (expired) + 2 recent
    _hits["test"] = [now - 120, now - 100, now - 70, now - 5, now - 1]
    _prune("test", now, window_s=60)
    assert len(_hits["test"]) == 2


def test_rate_limiter_is_limited():
    """Returns True when max_requests exceeded within window."""
    now = time.monotonic()
    _hits["test"] = [now - i for i in range(5)]
    assert _is_limited("test", now, max_requests=5, window_s=60) is True
    assert _is_limited("test", now, max_requests=10, window_s=60) is False


def test_rate_limiter_record():
    """Records a hit timestamp."""
    now = time.monotonic()
    _record("test", now)
    assert len(_hits["test"]) == 1
    assert _hits["test"][0] == now


def test_rate_limiter_sliding_window():
    """Window slides correctly — old entries expire, new ones count."""
    now = time.monotonic()
    # 3 requests 90s ago (outside 60s window) + 2 recent
    _hits["test"] = [now - 90, now - 85, now - 80, now - 10, now - 5]
    # With max=3, only 2 are in window → not limited
    assert _is_limited("test", now, max_requests=3, window_s=60) is False
    # With max=2, exactly 2 in window → limited
    assert _is_limited("test", now, max_requests=2, window_s=60) is True


# ============================================================
# Rate Limiter — Integration Tests
# ============================================================


@pytest.mark.anyio
@patch("app.routes.position_receipt.get_recent_transfers")
@patch("app.routes.position_receipt.estimate_first_seen")
@patch("app.routes.position_receipt.get_token_balance")
@patch("app.routes.position_receipt.resolve_token")
@patch("app.routes.position_receipt.get_token_price_cached")
async def test_rate_limit_per_ip(mock_price, mock_meta, mock_balance, mock_first_seen, mock_transfers, client):
    """Per-IP rate limiting triggers after exceeding threshold."""
    mock_balance.return_value = {"raw": 100, "decimals": 18, "formatted": "0.0000000000000001"}
    mock_meta.return_value = {"address": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed", "symbol": "DEGEN", "name": "Degen", "decimals": 18, "logo": None}
    mock_price.return_value = 0.03
    mock_first_seen.return_value = {"timestamp": None, "confidence": "low", "method": "mock", "scanWindow": "0", "note": "mocked"}
    mock_transfers.return_value = {"inbound": [], "outbound": [], "truncated": False}

    payload = {
        "address": "0x1234567890abcdef1234567890abcdef12345678",
        "token": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
    }

    # Override rate limit to a low value for testing
    with patch("app.middleware.rate_limit.RATE_LIMITS", {
        "per_ip": {"max_requests": 3, "window_s": 60},
        "per_wallet_token": {"max_requests": 100, "window_s": 60},
    }):
        reset_rate_limits()

        # First 3 requests should succeed
        for _ in range(3):
            resp = await client.post("/v1/position-receipt/base", json=payload)
            assert resp.status_code == 200

        # 4th request should be rate limited
        resp = await client.post("/v1/position-receipt/base", json=payload)
        assert resp.status_code == 429
        data = resp.json()
        assert data["error"] == "rate_limited"
        assert "Retry-After" in resp.headers


@pytest.mark.anyio
async def test_rate_limit_not_applied_to_get(client):
    """GET requests are not rate limited."""
    for _ in range(100):
        resp = await client.get("/v1/position-receipt/base")
        assert resp.status_code == 200


@pytest.mark.anyio
async def test_rate_limit_not_applied_to_health(client):
    """Health endpoint is not rate limited."""
    for _ in range(100):
        resp = await client.get("/health")
        assert resp.status_code == 200


# ============================================================
# Edge Case: Native Token (ETH)
# ============================================================


@pytest.mark.anyio
@patch("app.routes.position_receipt.get_recent_transfers")
@patch("app.routes.position_receipt.estimate_first_seen")
@patch("app.routes.position_receipt.get_token_balance")
@patch("app.routes.position_receipt.resolve_token")
@patch("app.routes.position_receipt.get_token_price_cached")
async def test_native_token_eth_skips_first_seen(mock_price, mock_meta, mock_balance, mock_first_seen, mock_transfers, client):
    """Native ETH skips first-seen and transfer scans."""
    mock_balance.return_value = {"raw": 1000000000000000000, "decimals": 18, "formatted": "1.0"}
    mock_meta.return_value = {"address": "0x0000000000000000000000000000000000000000", "symbol": "ETH", "name": "Ether", "decimals": 18, "logo": None}
    mock_price.return_value = 3000.0
    mock_transfers.return_value = {"inbound": [], "outbound": [], "truncated": False}

    resp = await client.post(
        "/v1/position-receipt/base",
        json={
            "address": "0x1234567890abcdef1234567890abcdef12345678",
            "token": "ETH",
        },
    )
    assert resp.status_code == 200
    data = resp.json()

    # First-seen should be skipped (not called)
    mock_first_seen.assert_not_called()
    assert data["firstSeenApprox"]["method"] == "skipped"
    assert data["firstSeenApprox"]["timestamp"] is None
    assert data["holdingDurationDays"] is None
    assert "native tokens" in data["firstSeenApprox"]["note"]

    # Recent transfers should also be skipped (not called)
    mock_transfers.assert_not_called()
    assert data["recentTransfers"]["inbound"] == []
    assert data["recentTransfers"]["outbound"] == []


@pytest.mark.anyio
@patch("app.routes.position_receipt.get_recent_transfers")
@patch("app.routes.position_receipt.estimate_first_seen")
@patch("app.routes.position_receipt.get_token_balance")
@patch("app.routes.position_receipt.resolve_token")
@patch("app.routes.position_receipt.get_token_price_cached")
async def test_native_token_sol_skips_first_seen(mock_price, mock_meta, mock_balance, mock_first_seen, mock_transfers, client):
    """Native SOL skips first-seen and transfer scans."""
    mock_balance.return_value = {"raw": 5000000000, "decimals": 9, "formatted": "5.0"}
    mock_meta.return_value = {"address": "So11111111111111111111111111111111111111112", "symbol": "SOL", "name": "Solana", "decimals": 9, "logo": None}
    mock_price.return_value = 150.0
    mock_transfers.return_value = {"inbound": [], "outbound": [], "truncated": False}

    resp = await client.post(
        "/v1/position-receipt/solana",
        json={
            "address": "DYw8jCTfwHNRJhhmFcbXvVDTqWMEVFBX6ZKUmG5CNSKK",
            "token": "SOL",
        },
    )
    assert resp.status_code == 200
    data = resp.json()

    mock_first_seen.assert_not_called()
    assert data["firstSeenApprox"]["method"] == "skipped"
    mock_transfers.assert_not_called()


# ============================================================
# Edge Case: Token Not Found → 404
# ============================================================


@pytest.mark.anyio
@patch("app.routes.position_receipt.get_token_price_cached")
@patch("app.routes.position_receipt.resolve_token", side_effect=Exception("Could not resolve Solana token FAKE — account not found"))
@patch("app.routes.position_receipt.get_token_balance")
async def test_token_not_found_solana(mock_balance, mock_meta, mock_price, client):
    """Token that doesn't exist returns 404."""
    resp = await client.post(
        "/v1/position-receipt/solana",
        json={
            "address": "DYw8jCTfwHNRJhhmFcbXvVDTqWMEVFBX6ZKUmG5CNSKK",
            "token": "FAKEtokenMintThatDoesNotExist11111111111111",
        },
    )
    assert resp.status_code == 404
    data = resp.json()
    assert data["error"] == "token_not_found"


@pytest.mark.anyio
@patch("app.routes.position_receipt.get_token_price_cached")
@patch("app.routes.position_receipt.resolve_token", side_effect=Exception("Could not resolve token metadata for 0xdead — no symbol returned. Token not found"))
@patch("app.routes.position_receipt.get_token_balance")
async def test_token_not_found_base(mock_balance, mock_meta, mock_price, client):
    """EVM token that doesn't exist returns 404."""
    resp = await client.post(
        "/v1/position-receipt/base",
        json={
            "address": "0x1234567890abcdef1234567890abcdef12345678",
            "token": "0xdeaddeaddeaddeaddeaddeaddeaddeaddeaddead",
        },
    )
    assert resp.status_code == 404
    data = resp.json()
    assert data["error"] == "token_not_found"


# ============================================================
# Edge Case: Upstream Error → 502
# ============================================================


@pytest.mark.anyio
@patch("app.routes.position_receipt.get_token_price_cached")
@patch("app.routes.position_receipt.resolve_token")
@patch("app.routes.position_receipt.get_token_balance", side_effect=Exception("RPC timeout"))
async def test_upstream_rpc_error(mock_balance, mock_meta, mock_price, client):
    """Generic upstream error returns 502."""
    resp = await client.post(
        "/v1/position-receipt/base",
        json={
            "address": "0x1234567890abcdef1234567890abcdef12345678",
            "token": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
        },
    )
    assert resp.status_code == 502
    assert resp.json()["error"] == "upstream_error"


@pytest.mark.anyio
@patch("app.routes.position_receipt.get_token_price_cached", side_effect=Exception("Price API down"))
@patch("app.routes.position_receipt.resolve_token")
@patch("app.routes.position_receipt.get_token_balance")
async def test_upstream_price_error_502(mock_balance, mock_meta, mock_price, client):
    """Price service crashing propagates as upstream_error."""
    resp = await client.post(
        "/v1/position-receipt/base",
        json={
            "address": "0x1234567890abcdef1234567890abcdef12345678",
            "token": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
        },
    )
    assert resp.status_code == 502
    assert resp.json()["error"] == "upstream_error"


# ============================================================
# Edge Case: First-seen failure → graceful degradation
# ============================================================


@pytest.mark.anyio
@patch("app.routes.position_receipt.get_recent_transfers")
@patch("app.routes.position_receipt.estimate_first_seen", side_effect=Exception("Scan timed out"))
@patch("app.routes.position_receipt.get_token_balance")
@patch("app.routes.position_receipt.resolve_token")
@patch("app.routes.position_receipt.get_token_price_cached")
async def test_first_seen_failure_graceful(mock_price, mock_meta, mock_balance, mock_first_seen, mock_transfers, client):
    """First-seen failure degrades gracefully — returns low confidence, no duration."""
    mock_balance.return_value = {"raw": 100, "decimals": 18, "formatted": "0.0000000000000001"}
    mock_meta.return_value = {"address": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed", "symbol": "DEGEN", "name": "Degen", "decimals": 18, "logo": None}
    mock_price.return_value = 0.03
    mock_transfers.return_value = {"inbound": [], "outbound": [], "truncated": False}

    resp = await client.post(
        "/v1/position-receipt/base",
        json={
            "address": "0x1234567890abcdef1234567890abcdef12345678",
            "token": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["firstSeenApprox"]["confidence"] == "low"
    assert data["firstSeenApprox"]["method"] == "error"
    assert data["holdingDurationDays"] is None
    assert "Scan timed out" in data["firstSeenApprox"]["note"]


# ============================================================
# Edge Case: Transfer fetch failure → graceful degradation
# ============================================================


@pytest.mark.anyio
@patch("app.routes.position_receipt.get_recent_transfers", side_effect=Exception("RPC error"))
@patch("app.routes.position_receipt.estimate_first_seen")
@patch("app.routes.position_receipt.get_token_balance")
@patch("app.routes.position_receipt.resolve_token")
@patch("app.routes.position_receipt.get_token_price_cached")
async def test_transfer_failure_graceful(mock_price, mock_meta, mock_balance, mock_first_seen, mock_transfers, client):
    """Transfer fetch failure returns empty transfers, not 500."""
    mock_balance.return_value = {"raw": 100, "decimals": 18, "formatted": "0.0000000000000001"}
    mock_meta.return_value = {"address": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed", "symbol": "DEGEN", "name": "Degen", "decimals": 18, "logo": None}
    mock_price.return_value = 0.03
    mock_first_seen.return_value = {"timestamp": None, "confidence": "low", "method": "mock", "scanWindow": "0", "note": "mocked"}

    resp = await client.post(
        "/v1/position-receipt/base",
        json={
            "address": "0x1234567890abcdef1234567890abcdef12345678",
            "token": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["recentTransfers"]["inbound"] == []
    assert data["recentTransfers"]["outbound"] == []
    assert data["lastTransferIn"] is None
    assert data["lastTransferOut"] is None


# ============================================================
# Error Response Shape
# ============================================================


@pytest.mark.anyio
async def test_error_response_shape(client):
    """All error responses have consistent shape."""
    resp = await client.post(
        "/v1/position-receipt/base",
        json={"address": "bad_addr", "token": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed"},
    )
    data = resp.json()
    assert "error" in data
    assert "message" in data
    assert "received_body" in data
    assert isinstance(data["error"], str)
    assert isinstance(data["message"], str)
    assert isinstance(data["received_body"], dict)


@pytest.mark.anyio
async def test_error_invalid_json(client):
    """Non-JSON body returns 400 with clear error."""
    resp = await client.post(
        "/v1/position-receipt/base",
        content="this is not json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_body"


@pytest.mark.anyio
async def test_error_array_body(client):
    """Array body (not object) returns 400."""
    resp = await client.post(
        "/v1/position-receipt/base",
        json=[1, 2, 3],
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_body"


# ============================================================
# Full Response Shape — Native Token
# ============================================================


@pytest.mark.anyio
@patch("app.routes.position_receipt.get_recent_transfers")
@patch("app.routes.position_receipt.estimate_first_seen")
@patch("app.routes.position_receipt.get_token_balance")
@patch("app.routes.position_receipt.resolve_token")
@patch("app.routes.position_receipt.get_token_price_cached")
async def test_native_eth_full_response(mock_price, mock_meta, mock_balance, mock_first_seen, mock_transfers, client):
    """Full response for native ETH has all required fields."""
    mock_balance.return_value = {"raw": 2500000000000000000, "decimals": 18, "formatted": "2.5"}
    mock_meta.return_value = {"address": "0x0000000000000000000000000000000000000000", "symbol": "ETH", "name": "Ether", "decimals": 18, "logo": None}
    mock_price.return_value = 3200.0
    mock_transfers.return_value = {"inbound": [], "outbound": [], "truncated": False}

    resp = await client.post(
        "/v1/position-receipt/base",
        json={
            "address": "0x1234567890abcdef1234567890abcdef12345678",
            "token": "0x0000000000000000000000000000000000000000",
        },
    )
    assert resp.status_code == 200
    data = resp.json()

    # All required fields present
    assert data["address"] == "0x1234567890abcdef1234567890abcdef12345678"
    assert data["chain"] == "base"
    assert data["token"]["symbol"] == "ETH"
    assert data["currentBalance"] == "2.5"
    assert data["currentValueUsd"] == 8000.0
    assert data["pricePerToken"] == 3200.0
    assert data["firstSeenApprox"]["method"] == "skipped"
    assert data["holdingDurationDays"] is None
    assert data["lastTransferIn"] is None
    assert data["lastTransferOut"] is None
    assert data["recentTransfers"] == {"inbound": [], "outbound": [], "truncated": False}
    assert data["flags"] is not None
    assert data["flagScope"] is not None
    assert data["notes"] is not None
    assert data["card"] is None
