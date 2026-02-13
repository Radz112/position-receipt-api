"""
Phase 3 Tests — Recent Transfer History (Capped + Early Exit)
Tests backward-chunked log fetch (Base), parallel batch parsing (Solana),
cap enforcement, early exit, truncation, and derive_last_transfers.
"""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.transfers import (
    get_recent_transfers_base,
    get_recent_transfers_solana,
    get_recent_transfers,
    derive_last_transfers,
    _parse_transfer_logs,
    _find_token_balance,
)
from app.utils.evm import unpad_address


# ============================================================
# Unit Tests — Helpers
# ============================================================


def test_unpad_address():
    padded = "0x0000000000000000000000001234567890abcdef1234567890abcdef12345678"
    assert unpad_address(padded) == "0x1234567890abcdef1234567890abcdef12345678"


def test_unpad_address_empty():
    assert unpad_address("") == ""
    assert unpad_address(None) == ""


def test_parse_transfer_logs_inbound():
    logs = [
        {
            "blockNumber": hex(1000),
            "transactionHash": "0xabc123",
            "data": hex(5000 * 10**18),  # 5000 tokens
            "topics": [
                "0xddf252ad...",
                "0x000000000000000000000000aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",  # from
                "0x000000000000000000000000bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",  # to
            ],
        }
    ]
    entries = _parse_transfer_logs(logs, 18, "in")
    assert len(entries) == 1
    assert entries[0]["txHash"] == "0xabc123"
    assert "from" in entries[0]
    assert entries[0]["from"] == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def test_parse_transfer_logs_outbound():
    logs = [
        {
            "blockNumber": hex(2000),
            "transactionHash": "0xdef456",
            "data": hex(100 * 10**6),  # 100 tokens (6 decimals)
            "topics": [
                "0xddf252ad...",
                "0x000000000000000000000000aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "0x000000000000000000000000bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            ],
        }
    ]
    entries = _parse_transfer_logs(logs, 6, "out")
    assert len(entries) == 1
    assert "to" in entries[0]
    assert "from" not in entries[0]


def test_find_token_balance():
    balances = [
        {"mint": "TOKEN_MINT_A", "uiTokenAmount": {"amount": "5000000"}},
        {"mint": "TOKEN_MINT_B", "uiTokenAmount": {"amount": "999"}},
    ]
    assert _find_token_balance(balances, "acc1", "TOKEN_MINT_A") == 5000000
    assert _find_token_balance(balances, "acc1", "TOKEN_MINT_B") == 999
    assert _find_token_balance(balances, "acc1", "UNKNOWN") == 0


def test_find_token_balance_empty():
    assert _find_token_balance([], "acc1", "MINT") == 0


# ============================================================
# derive_last_transfers
# ============================================================


def test_derive_last_transfers_both():
    recent = {
        "inbound": [{"timestamp": "2026-01-15", "amount": "100", "txHash": "0xA", "from": "0x1"}],
        "outbound": [{"timestamp": "2026-01-10", "amount": "50", "txHash": "0xB", "to": "0x2"}],
        "truncated": False,
    }
    last_in, last_out = derive_last_transfers(recent)
    assert last_in["txHash"] == "0xA"
    assert last_out["txHash"] == "0xB"


def test_derive_last_transfers_empty():
    last_in, last_out = derive_last_transfers({"inbound": [], "outbound": [], "truncated": False})
    assert last_in is None
    assert last_out is None


# ============================================================
# Base: Backward Chunked Fetch — Happy Path
# ============================================================


@pytest.mark.anyio
@patch("app.services.transfers.rpc")
async def test_base_transfers_found(mock_rpc):
    """Find inbound and outbound transfers with enough to meet targets."""
    mock_rpc.eth_block_number = AsyncMock(return_value=1_000_000)

    from_topic = "0x000000000000000000000000aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    to_topic = "0x000000000000000000000000bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    # Generate 5 inbound + 5 outbound in first chunk → targets met → early exit
    inbound_logs = [
        {"blockNumber": hex(999_000 - i), "transactionHash": f"0xin{i}", "data": hex(100 * 10**18), "topics": ["0x...", from_topic, to_topic]}
        for i in range(5)
    ]
    outbound_logs = [
        {"blockNumber": hex(998_000 - i), "transactionHash": f"0xout{i}", "data": hex(50 * 10**18), "topics": ["0x...", to_topic, from_topic]}
        for i in range(5)
    ]

    mock_rpc.eth_get_logs = AsyncMock(side_effect=[inbound_logs, outbound_logs])

    result = await get_recent_transfers_base("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "0xtoken", 18)

    assert len(result["inbound"]) == 5
    assert len(result["outbound"]) == 5
    assert result["truncated"] is False


# ============================================================
# Base: RPC Cap → Truncation
# ============================================================


@pytest.mark.anyio
@patch("app.services.transfers.rpc")
async def test_base_transfers_rpc_cap(mock_rpc):
    """Hitting RPC cap sets truncated=True."""
    mock_rpc.eth_block_number = AsyncMock(return_value=500_000)
    mock_rpc.eth_get_logs = AsyncMock(return_value=[])  # Empty results each time

    result = await get_recent_transfers_base("0x" + "1" * 40, "0x" + "2" * 40, 18)

    # Should have stopped at max_rpc_calls (10)
    # 1 call for block number + up to 2 per chunk (inbound + outbound)
    total_calls = mock_rpc.eth_block_number.call_count + mock_rpc.eth_get_logs.call_count
    assert total_calls <= 10
    assert result["truncated"] is True


# ============================================================
# Base: Early Exit When Targets Met
# ============================================================


@pytest.mark.anyio
@patch("app.services.transfers.rpc")
async def test_base_transfers_early_exit(mock_rpc):
    """Stop scanning when both inbound and outbound targets are met."""
    mock_rpc.eth_block_number = AsyncMock(return_value=1_000_000)

    from_topic = "0x" + "a" * 64
    to_topic = "0x" + "b" * 64

    # Generate 5 inbound + 5 outbound in first chunk
    inbound_logs = [
        {"blockNumber": hex(999_000 - i), "transactionHash": f"0xin{i}", "data": hex(10 * 10**18), "topics": ["0x...", from_topic, to_topic]}
        for i in range(5)
    ]
    outbound_logs = [
        {"blockNumber": hex(998_000 - i), "transactionHash": f"0xout{i}", "data": hex(5 * 10**18), "topics": ["0x...", to_topic, from_topic]}
        for i in range(5)
    ]

    mock_rpc.eth_get_logs = AsyncMock(side_effect=[inbound_logs, outbound_logs])

    result = await get_recent_transfers_base("0x" + "b" * 40, "0xtoken", 18)

    assert len(result["inbound"]) == 5
    assert len(result["outbound"]) == 5
    assert result["truncated"] is False
    # Should only have made 3 calls: blockNumber + 1 inbound + 1 outbound
    assert mock_rpc.eth_get_logs.call_count == 2


# ============================================================
# Solana: Happy Path
# ============================================================


@pytest.mark.anyio
@patch("app.services.transfers.rpc")
async def test_solana_transfers_found(mock_rpc):
    """Parse Solana transactions into inbound/outbound."""
    mock_rpc.solana_get_token_accounts_by_owner = AsyncMock(return_value={
        "value": [{"pubkey": "TokenAcc111"}],
    })
    mock_rpc.solana_get_signatures_for_address = AsyncMock(return_value=[
        {"signature": "sig1"},
        {"signature": "sig2"},
    ])

    # sig1: inbound (post > pre), sig2: outbound (post < pre)
    mock_rpc.solana_get_transaction = AsyncMock(side_effect=[
        {
            "blockTime": 1700000000,
            "meta": {
                "preTokenBalances": [{"mint": "MINT_A", "uiTokenAmount": {"amount": "0"}}],
                "postTokenBalances": [{"mint": "MINT_A", "uiTokenAmount": {"amount": "1000000"}}],
            },
            "transaction": {
                "signatures": ["sig1"],
                "message": {"instructions": []},
            },
        },
        {
            "blockTime": 1700001000,
            "meta": {
                "preTokenBalances": [{"mint": "MINT_A", "uiTokenAmount": {"amount": "1000000"}}],
                "postTokenBalances": [{"mint": "MINT_A", "uiTokenAmount": {"amount": "500000"}}],
            },
            "transaction": {
                "signatures": ["sig2"],
                "message": {"instructions": []},
            },
        },
    ])

    result = await get_recent_transfers_solana("Owner111", "MINT_A", 6)

    assert len(result["inbound"]) == 1
    assert len(result["outbound"]) == 1
    assert result["inbound"][0]["txHash"] == "sig1"
    assert result["outbound"][0]["txHash"] == "sig2"
    assert result["truncated"] is False


# ============================================================
# Solana: No Token Account
# ============================================================


@pytest.mark.anyio
@patch("app.services.transfers.rpc")
async def test_solana_transfers_no_account(mock_rpc):
    mock_rpc.solana_get_token_accounts_by_owner = AsyncMock(return_value={"value": []})
    result = await get_recent_transfers_solana("Owner111", "MINT_A", 6)
    assert result == {"inbound": [], "outbound": [], "truncated": False}


# ============================================================
# Solana: Tx Parse Cap → Truncation
# ============================================================


@pytest.mark.anyio
@patch("app.services.transfers.rpc")
async def test_solana_transfers_tx_cap(mock_rpc):
    """Hitting max_tx_parsed sets truncated=True."""
    mock_rpc.solana_get_token_accounts_by_owner = AsyncMock(return_value={
        "value": [{"pubkey": "TokenAcc111"}],
    })
    # Return 30 signatures (max sig_fetch_limit)
    mock_rpc.solana_get_signatures_for_address = AsyncMock(return_value=[
        {"signature": f"sig{i}"} for i in range(30)
    ])
    # All transactions have zero diff (no actual transfers) so targets never met
    mock_rpc.solana_get_transaction = AsyncMock(return_value={
        "blockTime": 1700000000,
        "meta": {
            "preTokenBalances": [{"mint": "MINT_A", "uiTokenAmount": {"amount": "1000"}}],
            "postTokenBalances": [{"mint": "MINT_A", "uiTokenAmount": {"amount": "1000"}}],
        },
        "transaction": {
            "signatures": ["sigX"],
            "message": {"instructions": []},
        },
    })

    result = await get_recent_transfers_solana("Owner111", "MINT_A", 6)

    # max_tx_parsed = 20, parallel_batch_size = 5, so 4 batches of 5 = 20 parsed
    assert mock_rpc.solana_get_transaction.call_count == 20
    assert result["truncated"] is True


# ============================================================
# Dispatcher
# ============================================================


@pytest.mark.anyio
@patch("app.services.transfers.get_recent_transfers_base")
async def test_dispatcher_base(mock_fn):
    mock_fn.return_value = {"inbound": [], "outbound": [], "truncated": False}
    result = await get_recent_transfers("base", "0x" + "1" * 40, "0x" + "2" * 40, 18)
    assert result["truncated"] is False
    mock_fn.assert_called_once()


@pytest.mark.anyio
@patch("app.services.transfers.get_recent_transfers_solana")
async def test_dispatcher_solana(mock_fn):
    mock_fn.return_value = {"inbound": [], "outbound": [], "truncated": False}
    result = await get_recent_transfers("solana", "addr", "mint", 9)
    mock_fn.assert_called_once()


@pytest.mark.anyio
async def test_dispatcher_unsupported():
    result = await get_recent_transfers("polygon", "addr", "tok", 18)
    assert result == {"inbound": [], "outbound": [], "truncated": False}
