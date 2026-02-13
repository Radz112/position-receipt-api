"""
Phase 1 Tests — Balance + Token Metadata + Price
Tests the APIX middleware, param extraction, validation, and service integration.
"""

import json
import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport

from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ============================================================
# Health Check
# ============================================================


@pytest.mark.anyio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ============================================================
# GET /v1/position-receipt/{chain} — Info Endpoint
# ============================================================


@pytest.mark.anyio
async def test_get_info_base(client):
    resp = await client.get("/v1/position-receipt/base")
    assert resp.status_code == 200
    data = resp.json()
    assert data["chain"] == "base"
    assert data["method"] == "POST"
    assert "address" in data["parameters"]
    assert "token" in data["parameters"]


@pytest.mark.anyio
async def test_get_info_solana(client):
    resp = await client.get("/v1/position-receipt/solana")
    assert resp.status_code == 200
    data = resp.json()
    assert data["chain"] == "solana"


@pytest.mark.anyio
async def test_get_info_invalid_chain(client):
    resp = await client.get("/v1/position-receipt/ethereum")
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"] == "invalid_chain"


# ============================================================
# POST Validation
# ============================================================


@pytest.mark.anyio
async def test_post_missing_address(client):
    resp = await client.post(
        "/v1/position-receipt/base",
        json={"token": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed"},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"] == "missing_address"
    assert "received_body" in data


@pytest.mark.anyio
async def test_post_missing_token(client):
    resp = await client.post(
        "/v1/position-receipt/base",
        json={"address": "0x1234567890abcdef1234567890abcdef12345678"},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"] == "missing_token"


@pytest.mark.anyio
async def test_post_invalid_address_base(client):
    resp = await client.post(
        "/v1/position-receipt/base",
        json={"address": "not-an-address", "token": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_address"


@pytest.mark.anyio
async def test_post_invalid_token_base(client):
    resp = await client.post(
        "/v1/position-receipt/base",
        json={"address": "0x1234567890abcdef1234567890abcdef12345678", "token": "bad"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_token"


@pytest.mark.anyio
async def test_post_invalid_depth(client):
    resp = await client.post(
        "/v1/position-receipt/base",
        json={
            "address": "0x1234567890abcdef1234567890abcdef12345678",
            "token": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
            "depth": "extreme",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_depth"


@pytest.mark.anyio
async def test_post_invalid_chain(client):
    resp = await client.post(
        "/v1/position-receipt/polygon",
        json={
            "address": "0x1234567890abcdef1234567890abcdef12345678",
            "token": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_chain"


@pytest.mark.anyio
async def test_post_invalid_body(client):
    resp = await client.post(
        "/v1/position-receipt/base",
        content="not json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_body"


# ============================================================
# APIX Body Unwrapper Middleware
# ============================================================


@pytest.mark.anyio
@patch("app.routes.position_receipt.estimate_first_seen")
@patch("app.routes.position_receipt.get_token_balance")
@patch("app.routes.position_receipt.resolve_token")
@patch("app.routes.position_receipt.get_token_price_cached")
async def test_body_unwrapper(mock_price, mock_meta, mock_balance, mock_first_seen, client):
    """APIX agent nests payload inside body.body — middleware should unwrap."""
    mock_balance.return_value = {"raw": 1000000, "decimals": 18, "formatted": "0.000000000001"}
    mock_meta.return_value = {
        "address": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
        "symbol": "DEGEN",
        "name": "Degen",
        "decimals": 18,
        "logo": None,
    }
    mock_price.return_value = 0.03
    mock_first_seen.return_value = {"timestamp": None, "confidence": "low", "method": "mock", "scanWindow": "0", "note": "mocked"}

    # Nested body format that APIX agent sends
    resp = await client.post(
        "/v1/position-receipt/base",
        json={
            "body": {
                "address": "0x1234567890abcdef1234567890abcdef12345678",
                "token": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
            }
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["address"] == "0x1234567890abcdef1234567890abcdef12345678"
    assert data["token"]["symbol"] == "DEGEN"


# ============================================================
# APIX Triple-Location Param Extraction
# ============================================================


@pytest.mark.anyio
@patch("app.routes.position_receipt.estimate_first_seen")
@patch("app.routes.position_receipt.get_token_balance")
@patch("app.routes.position_receipt.resolve_token")
@patch("app.routes.position_receipt.get_token_price_cached")
async def test_query_field_extraction(mock_price, mock_meta, mock_balance, mock_first_seen, client):
    """APIX agent may put address in 'query' field."""
    mock_balance.return_value = {"raw": 0, "decimals": 18, "formatted": "0"}
    mock_meta.return_value = {
        "address": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
        "symbol": "DEGEN",
        "name": "Degen",
        "decimals": 18,
        "logo": None,
    }
    mock_price.return_value = None
    mock_first_seen.return_value = {"timestamp": None, "confidence": "low", "method": "mock", "scanWindow": "0", "note": "mocked"}

    resp = await client.post(
        "/v1/position-receipt/base",
        json={
            "query": "0x1234567890abcdef1234567890abcdef12345678",
            "token": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["address"] == "0x1234567890abcdef1234567890abcdef12345678"


@pytest.mark.anyio
@patch("app.routes.position_receipt.estimate_first_seen")
@patch("app.routes.position_receipt.get_token_balance")
@patch("app.routes.position_receipt.resolve_token")
@patch("app.routes.position_receipt.get_token_price_cached")
async def test_alias_extraction(mock_price, mock_meta, mock_balance, mock_first_seen, client):
    """Parameters can be sent with aliases like 'wallet' or 'mint'."""
    mock_balance.return_value = {"raw": 5000, "decimals": 18, "formatted": "0.000000000000005"}
    mock_meta.return_value = {
        "address": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
        "symbol": "DEGEN",
        "name": "Degen",
        "decimals": 18,
        "logo": None,
    }
    mock_price.return_value = 0.03
    mock_first_seen.return_value = {"timestamp": None, "confidence": "low", "method": "mock", "scanWindow": "0", "note": "mocked"}

    resp = await client.post(
        "/v1/position-receipt/base",
        json={
            "wallet": "0x1234567890abcdef1234567890abcdef12345678",
            "mint": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["address"] == "0x1234567890abcdef1234567890abcdef12345678"


# ============================================================
# POST Success — Mocked Services
# ============================================================


@pytest.mark.anyio
@patch("app.routes.position_receipt.estimate_first_seen")
@patch("app.routes.position_receipt.get_token_balance")
@patch("app.routes.position_receipt.resolve_token")
@patch("app.routes.position_receipt.get_token_price_cached")
async def test_post_success_base(mock_price, mock_meta, mock_balance, mock_first_seen, client):
    mock_balance.return_value = {"raw": 42069500000000000000000, "decimals": 18, "formatted": "42069.5"}
    mock_meta.return_value = {
        "address": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
        "symbol": "DEGEN",
        "name": "Degen",
        "decimals": 18,
        "logo": None,
    }
    mock_price.return_value = 0.0297
    mock_first_seen.return_value = {
        "timestamp": "2025-11-20T14:30:00Z",
        "confidence": "medium",
        "method": "chunked_log_scan",
        "scanWindow": "90 days",
        "note": "Based on first Transfer event within 90-day window",
    }

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

    assert data["address"] == "0x1234567890abcdef1234567890abcdef12345678"
    assert data["chain"] == "base"
    assert data["token"]["symbol"] == "DEGEN"
    assert data["currentBalance"] == "42069.5"
    assert data["pricePerToken"] == 0.0297
    assert data["currentValueUsd"] == round(42069.5 * 0.0297, 2)
    # Phase 2 fields
    assert data["firstSeenApprox"]["confidence"] == "medium"
    assert data["firstSeenApprox"]["timestamp"] == "2025-11-20T14:30:00Z"
    assert data["holdingDurationDays"] is not None
    assert data["holdingDurationDays"] > 0
    assert data["flags"] == []
    assert data["card"] is None


@pytest.mark.anyio
@patch("app.routes.position_receipt.estimate_first_seen")
@patch("app.routes.position_receipt.get_token_balance")
@patch("app.routes.position_receipt.resolve_token")
@patch("app.routes.position_receipt.get_token_price_cached")
async def test_post_success_solana(mock_price, mock_meta, mock_balance, mock_first_seen, client):
    mock_balance.return_value = {"raw": 100000000000, "decimals": 9, "formatted": "100.0"}
    mock_meta.return_value = {
        "address": "So11111111111111111111111111111111111111112",
        "symbol": "SOL",
        "name": "Solana",
        "decimals": 9,
        "logo": None,
    }
    mock_price.return_value = 150.0
    mock_first_seen.return_value = {"timestamp": None, "confidence": "low", "method": "mock", "scanWindow": "0", "note": "mocked"}

    resp = await client.post(
        "/v1/position-receipt/solana",
        json={
            "address": "DYw8jCTfwHNRJhhmFcbXvVDTqWMEVFBX6ZKUmG5CNSKK",
            "token": "So11111111111111111111111111111111111111112",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["chain"] == "solana"
    assert data["currentBalance"] == "100.0"
    assert data["currentValueUsd"] == 15000.0


@pytest.mark.anyio
@patch("app.routes.position_receipt.estimate_first_seen")
@patch("app.routes.position_receipt.get_token_balance")
@patch("app.routes.position_receipt.resolve_token")
@patch("app.routes.position_receipt.get_token_price_cached")
async def test_post_price_null(mock_price, mock_meta, mock_balance, mock_first_seen, client):
    """Price timeout/failure returns null values gracefully."""
    mock_balance.return_value = {"raw": 1000, "decimals": 18, "formatted": "0.000000000000001"}
    mock_meta.return_value = {
        "address": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
        "symbol": "DEGEN",
        "name": "Degen",
        "decimals": 18,
        "logo": None,
    }
    mock_price.return_value = None  # Price fetch failed
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
    assert data["pricePerToken"] is None
    assert data["currentValueUsd"] is None


@pytest.mark.anyio
@patch("app.routes.position_receipt.estimate_first_seen")
@patch("app.routes.position_receipt.get_token_balance")
@patch("app.routes.position_receipt.resolve_token")
@patch("app.routes.position_receipt.get_token_price_cached")
async def test_post_zero_balance(mock_price, mock_meta, mock_balance, mock_first_seen, client):
    """Zero balance still returns a valid response."""
    mock_balance.return_value = {"raw": 0, "decimals": 18, "formatted": "0"}
    mock_meta.return_value = {
        "address": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
        "symbol": "DEGEN",
        "name": "Degen",
        "decimals": 18,
        "logo": None,
    }
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
    assert data["currentBalance"] == "0"
    assert data["currentValueUsd"] == 0.0


# ============================================================
# Error Response Debug Payload
# ============================================================


@pytest.mark.anyio
async def test_error_includes_received_body(client):
    """All error responses include received_body for APIX debugging."""
    resp = await client.post(
        "/v1/position-receipt/base",
        json={"address": "bad", "token": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed"},
    )
    data = resp.json()
    assert "received_body" in data
    assert "address" in data["received_body"]


# ============================================================
# Upstream Error Handling
# ============================================================


@pytest.mark.anyio
@patch("app.routes.position_receipt.get_token_balance", side_effect=Exception("RPC timeout"))
@patch("app.routes.position_receipt.resolve_token")
@patch("app.routes.position_receipt.get_token_price_cached")
async def test_upstream_error(mock_price, mock_meta, mock_balance, client):
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
# Unit Tests — extract_param
# ============================================================


def test_extract_param_direct():
    from app.utils.params import extract_param

    body = {"address": "0xABC"}
    assert extract_param(body, "address") == "0xABC"


def test_extract_param_alias():
    from app.utils.params import extract_param

    body = {"wallet": "0xABC"}
    assert extract_param(body, "address", aliases=["wallet"]) == "0xABC"


def test_extract_param_nested():
    from app.utils.params import extract_param

    body = {"body": {"address": "0xABC"}}
    assert extract_param(body, "address") == "0xABC"


def test_extract_param_query_fallback():
    from app.utils.params import extract_param

    body = {"query": "0xABC"}
    assert extract_param(body, "address", use_query_fallback=True) == "0xABC"
    # Without use_query_fallback, should NOT fall through to query
    assert extract_param(body, "depth") is None


def test_extract_param_not_found():
    from app.utils.params import extract_param

    body = {"foo": "bar"}
    assert extract_param(body, "address") is None


# ============================================================
# Unit Tests — Validation
# ============================================================


def test_validate_chain():
    from app.utils.validation import validate_chain

    assert validate_chain("base") is None
    assert validate_chain("solana") is None
    assert validate_chain("ethereum") is not None


def test_validate_address_base():
    from app.utils.validation import validate_address

    assert validate_address("base", "0x1234567890abcdef1234567890abcdef12345678") is None
    assert validate_address("base", "bad") is not None


def test_validate_address_solana():
    from app.utils.validation import validate_address

    assert validate_address("solana", "DYw8jCTfwHNRJhhmFcbXvVDTqWMEVFBX6ZKUmG5CNSKK") is None
    assert validate_address("solana", "0xinvalid") is not None


def test_validate_depth():
    from app.utils.validation import validate_depth

    assert validate_depth("fast") is None
    assert validate_depth("standard") is None
    assert validate_depth("deep") is None
    assert validate_depth("extreme") is not None


# ============================================================
# Unit Tests — Circuit Breaker
# ============================================================


def test_circuit_breaker_initial_state():
    from app.services.price import _circuit

    assert _circuit["jupiter"]["open"] is False
    assert _circuit["dexscreener"]["open"] is False
