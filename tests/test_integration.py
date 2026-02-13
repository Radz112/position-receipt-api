"""Integration tests — zero mocks, real code paths."""

import json
import time
from datetime import datetime, timezone, timedelta

import pytest
from app.utils.evm import pad_address, unpad_address
from app.services.token_metadata import _decode_string
from app.utils.params import extract_param
from app.utils.validation import validate_chain, validate_address, validate_token, validate_depth
from app.utils.errors import error_response
from app.services.confidence import parse_iso, detect_flags, generate_notes, build_flag_scope
from app.services.transfers import _parse_transfer_logs, _find_token_balance, derive_last_transfers
from app.middleware.rate_limit import _prune, _is_limited, _record, _hits, reset_rate_limits
from app.services.price import _circuit, _circuit_open, _trip_circuit, CIRCUIT_OPEN_DURATION
from app.services.first_seen import _budget_exceeded
from app.services.balance import _encode_address


def _abi_encode_string(s: str) -> str:
    encoded = s.encode("utf-8")
    offset = (32).to_bytes(32, "big")
    length = len(encoded).to_bytes(32, "big")
    pad_len = (32 - len(encoded) % 32) % 32
    return "0x" + (offset + length + encoded + b"\x00" * pad_len).hex()


ADDR_40 = "abcdef1234567890abcdef1234567890abcdef12"
PADDED_ADDR = "000000000000000000000000" + ADDR_40


class TestPadAddress:
    def test_standard(self):
        assert pad_address("0x" + ADDR_40) == "0x" + PADDED_ADDR

    def test_uppercase(self):
        assert pad_address("0x" + ADDR_40.upper()) == "0x" + PADDED_ADDR

    def test_no_0x_prefix(self):
        assert pad_address(ADDR_40) == "0x" + PADDED_ADDR

    def test_already_padded(self):
        assert pad_address("0x" + PADDED_ADDR) == "0x" + PADDED_ADDR

    def test_short_address(self):
        assert pad_address("0xabc") == "0x" + "0" * 61 + "abc"


class TestUnpadAddress:
    def test_standard(self):
        assert unpad_address("0x" + PADDED_ADDR) == "0x" + ADDR_40

    def test_uppercase(self):
        assert unpad_address("0x" + PADDED_ADDR.upper()) == "0x" + ADDR_40.upper()

    def test_short_input(self):
        # [-40:] on a 5-char string returns the whole string
        assert unpad_address("0xabc") == "0x0xabc"

    def test_empty_string(self):
        assert unpad_address("") == ""

    def test_no_0x_prefix(self):
        assert unpad_address(PADDED_ADDR) == "0x" + ADDR_40


class TestDecodeString:
    def test_none(self):
        assert _decode_string(None) == ""

    def test_empty(self):
        assert _decode_string("") == ""

    def test_0x_only(self):
        assert _decode_string("0x") == ""

    def test_too_short(self):
        assert _decode_string("0x1234") == ""

    def test_valid_short(self):
        assert _decode_string(_abi_encode_string("USDC")) == "USDC"

    def test_valid_long(self):
        assert _decode_string(_abi_encode_string("Wrapped Ether")) == "Wrapped Ether"

    def test_bytes32_under_length_guard(self):
        # 32 bytes = 66 chars with 0x prefix, under the 130-char guard
        raw = "TOKEN".encode("utf-8").ljust(32, b"\x00")
        assert _decode_string("0x" + raw.hex()) == ""

    def test_garbage_hex_returns_empty(self):
        # ABI path interprets huge offset → length=0 → ""
        assert _decode_string("0x" + "ff" * 64) == ""

    def test_null_byte_padding_stripped(self):
        assert _decode_string(_abi_encode_string("DAI\x00\x00")) == "DAI"


class TestExtractParam:
    def test_direct_body_wins_over_nested(self):
        body = {"address": "0xabc", "body": {"address": "0xother"}}
        assert extract_param(body, "address") == "0xabc"

    def test_nested_body_fallback(self):
        body = {"body": {"address": "0xnested"}}
        assert extract_param(body, "address") == "0xnested"

    def test_alias_direct(self):
        assert extract_param({"wallet": "0xw"}, "address", aliases=["wallet"]) == "0xw"

    def test_alias_nested(self):
        body = {"body": {"wallet": "0xw"}}
        assert extract_param(body, "address", aliases=["wallet"]) == "0xw"

    def test_query_fallback_enabled(self):
        assert extract_param({"query": "0xq"}, "address", use_query_fallback=True) == "0xq"

    def test_query_fallback_disabled(self):
        assert extract_param({"query": "0xq"}, "address", use_query_fallback=False) is None

    def test_non_dict_nested_body(self):
        assert extract_param({"body": 42}, "address") is None

    def test_non_string_return(self):
        assert extract_param({"depth": 5}, "depth") == 5

    def test_not_found(self):
        assert extract_param({"foo": "bar"}, "address") is None


class TestValidateChain:
    def test_valid_base(self):
        assert validate_chain("base") is None

    def test_valid_solana(self):
        assert validate_chain("solana") is None

    def test_empty(self):
        assert validate_chain("") is not None

    def test_wrong_case(self):
        assert validate_chain("Base") is not None

    def test_whitespace(self):
        assert validate_chain(" base") is not None


class TestValidateAddressBase:
    def test_valid(self):
        assert validate_address("base", "0x" + "a" * 40) is None

    def test_uppercase_valid(self):
        assert validate_address("base", "0x" + "A" * 40) is None

    def test_too_short(self):
        assert validate_address("base", "0x" + "a" * 39) is not None

    def test_too_long(self):
        assert validate_address("base", "0x" + "a" * 41) is not None

    def test_no_0x(self):
        assert validate_address("base", "a" * 40) is not None

    def test_0x_only(self):
        assert validate_address("base", "0x") is not None

    def test_leading_whitespace(self):
        assert validate_address("base", " 0x" + "a" * 40) is not None

    def test_invalid_hex_char(self):
        assert validate_address("base", "0x" + "g" * 40) is not None

    def test_empty(self):
        assert "required" in validate_address("base", "").lower()


class TestValidateAddressSolana:
    def test_32_chars(self):
        assert validate_address("solana", "A" * 32) is None

    def test_44_chars(self):
        assert validate_address("solana", "B" * 44) is None

    def test_31_chars(self):
        assert validate_address("solana", "A" * 31) is not None

    def test_45_chars(self):
        assert validate_address("solana", "A" * 45) is not None

    def test_invalid_base58_zero(self):
        # '0' not in base58 alphabet [1-9A-HJ-NP-Za-km-z]
        assert validate_address("solana", "0" + "A" * 31) is not None

    def test_invalid_base58_I(self):
        assert validate_address("solana", "I" + "A" * 31) is not None

    def test_invalid_base58_O(self):
        assert validate_address("solana", "O" + "A" * 31) is not None

    def test_invalid_base58_l(self):
        assert validate_address("solana", "l" + "A" * 31) is not None

    def test_empty(self):
        assert validate_address("solana", "") is not None


class TestValidateToken:
    def test_eth(self):
        assert validate_token("base", "ETH") is None

    def test_eth_lowercase(self):
        assert validate_token("base", "eth") is None

    def test_sol(self):
        assert validate_token("solana", "SOL") is None

    def test_eth_on_solana_accepted(self):
        # No chain guard — matches token.lower() in ("eth", "sol")
        assert validate_token("solana", "eth") is None

    def test_empty(self):
        assert "required" in validate_token("base", "").lower()

    def test_valid_base_contract(self):
        assert validate_token("base", "0x" + "a" * 40) is None

    def test_invalid_base_contract(self):
        assert validate_token("base", "0xinvalid") is not None

    def test_valid_solana_mint(self):
        assert validate_token("solana", "A" * 44) is None

    def test_invalid_solana_mint(self):
        assert validate_token("solana", "0xinvalid") is not None


class TestValidateDepth:
    def test_valid(self):
        assert validate_depth("fast") is None
        assert validate_depth("standard") is None
        assert validate_depth("deep") is None

    def test_case_sensitive(self):
        assert validate_depth("FAST") is not None

    def test_trailing_space(self):
        assert validate_depth("fast ") is not None

    def test_empty(self):
        assert validate_depth("") is not None


class TestErrorResponse:
    def test_basic_shape(self):
        resp = error_response(400, "test_error", "Something went wrong", {"key": "val"})
        assert resp.status_code == 400
        content = json.loads(resp.body.decode())
        assert content["error"] == "test_error"
        assert content["message"] == "Something went wrong"
        assert content["received_body"]["key"] == "val"

    def test_none_body(self):
        content = json.loads(error_response(400, "e", "m", None).body.decode())
        assert content["received_body"] == {}

    def test_long_value_truncated(self):
        content = json.loads(error_response(400, "e", "m", {"a": "x" * 300}).body.decode())
        assert len(content["received_body"]["a"]) == 200

    def test_non_string_cast(self):
        content = json.loads(error_response(400, "e", "m", {"n": 42}).body.decode())
        assert content["received_body"]["n"] == "42"


class TestParseIso:
    def test_with_z(self):
        dt = parse_iso("2024-01-15T12:00:00Z")
        assert dt == datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

    def test_without_z(self):
        dt = parse_iso("2024-01-15T12:00:00")
        assert dt.tzinfo == timezone.utc

    def test_fractional_seconds(self):
        assert parse_iso("2024-01-15T12:00:00.123456Z").microsecond == 123456

    def test_empty_raises(self):
        with pytest.raises(Exception):
            parse_iso("")

    def test_none_raises(self):
        with pytest.raises(Exception):
            parse_iso(None)


class TestDetectFlags:
    @staticmethod
    def _base(**overrides):
        defaults = {
            "balance": {"formatted": "100.0"},
            "value_usd": 500.0,
            "first_seen": {"timestamp": None, "confidence": "low"},
            "recent_transfers": {"inbound": [], "outbound": []},
            "token_info": {"address": "0x" + "a" * 40, "symbol": "TEST"},
            "chain": "base",
        }
        defaults.update(overrides)
        return defaults

    def test_zero_balance(self):
        assert detect_flags(**self._base(balance={"formatted": "0"})) == ["zero_balance"]

    def test_dust_below(self):
        assert "dust_amount" in detect_flags(**self._base(value_usd=0.99))

    def test_dust_exact(self):
        assert "dust_amount" not in detect_flags(**self._base(value_usd=1.0))

    def test_large_holder_above(self):
        assert "large_holder" in detect_flags(**self._base(value_usd=10000.01))

    def test_large_holder_exact(self):
        assert "large_holder" not in detect_flags(**self._base(value_usd=10000))

    def test_recently_acquired(self):
        ts = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat() + "Z"
        flags = detect_flags(**self._base(first_seen={"timestamp": ts, "confidence": "medium"}))
        assert "recently_acquired" in flags

    def test_not_recently_acquired_7_days(self):
        ts = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat() + "Z"
        flags = detect_flags(**self._base(first_seen={"timestamp": ts, "confidence": "medium"}))
        assert "recently_acquired" not in flags

    def test_value_none_skips_value_flags(self):
        flags = detect_flags(**self._base(value_usd=None))
        assert "dust_amount" not in flags
        assert "large_holder" not in flags

    def test_frequent_trader_at_9(self):
        t = {"inbound": [{"from": "x"}] * 5, "outbound": [{"to": "x"}] * 4}
        assert "frequent_trader" not in detect_flags(**self._base(recent_transfers=t))

    def test_frequent_trader_at_10(self):
        t = {"inbound": [{"from": "x"}] * 5, "outbound": [{"to": "x"}] * 5}
        assert "frequent_trader" in detect_flags(**self._base(recent_transfers=t))

    def test_single_transfer_in(self):
        t = {"inbound": [{"from": "x"}], "outbound": []}
        assert "single_transfer_in" in detect_flags(**self._base(recent_transfers=t))

    def test_multiple_inflows(self):
        t = {"inbound": [{"from": "x"}] * 3, "outbound": []}
        assert "multiple_inflows" in detect_flags(**self._base(recent_transfers=t))

    def test_unknown_chain_no_crash(self):
        assert "dex_router_source" not in detect_flags(**self._base(chain="ethereum"))

    def test_multiple_flags(self):
        ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat() + "Z"
        t = {"inbound": [{"from": "x"}] * 5, "outbound": [{"to": "x"}] * 5}
        flags = detect_flags(**self._base(
            value_usd=0.50,
            first_seen={"timestamp": ts, "confidence": "high"},
            recent_transfers=t,
        ))
        assert "dust_amount" in flags
        assert "recently_acquired" in flags
        assert "frequent_trader" in flags

    def test_dex_router(self):
        t = {"inbound": [{"from": "0x2626664c2603336e57b271c5c0b26f421741e481"}], "outbound": []}
        assert "dex_router_source" in detect_flags(**self._base(recent_transfers=t, chain="base"))

    def test_wrapped_token(self):
        info = {"address": "0x4200000000000000000000000000000000000006", "symbol": "WETH"}
        assert "wrapped_token" in detect_flags(**self._base(token_info=info, chain="base"))

    def test_lp_token(self):
        info = {"address": "0x" + "b" * 40, "symbol": "UNI-V2"}
        assert "lp_token" in detect_flags(**self._base(token_info=info))

    def test_low_confidence_skips_recently_acquired(self):
        ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat() + "Z"
        flags = detect_flags(**self._base(first_seen={"timestamp": ts, "confidence": "low"}))
        assert "recently_acquired" not in flags


class TestGenerateNotes:
    def test_zero_balance(self):
        notes = generate_notes(["zero_balance"], {"timestamp": None}, {"inbound": [], "outbound": []}, {"formatted": "0"})
        assert len(notes) == 1
        assert "zero" in notes[0].lower()

    def test_all_flags(self):
        ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat() + "Z"
        notes = generate_notes(
            ["multiple_inflows", "possible_airdrop", "recently_acquired",
             "frequent_trader", "dex_router_source", "lp_token", "wrapped_token"],
            {"timestamp": ts, "confidence": "medium"},
            {"inbound": [], "outbound": [{"amount": "50.0"}], "truncated": True},
            {"formatted": "100.0"},
        )
        assert len(notes) == 9
        assert any("multiple transactions" in n for n in notes)
        assert any("airdrop" in n for n in notes)
        assert any("days ago" in n for n in notes)
        assert any("frequency" in n.lower() for n in notes)
        assert any("DEX swap" in n for n in notes)
        assert any("transferred out" in n for n in notes)
        assert any("truncated" in n for n in notes)
        assert any("liquidity pool" in n for n in notes)
        assert any("wrapped" in n.lower() for n in notes)

    def test_0_days_ago(self):
        ts = datetime.now(timezone.utc).isoformat() + "Z"
        notes = generate_notes(["recently_acquired"], {"timestamp": ts, "confidence": "medium"}, {"inbound": [], "outbound": []}, {"formatted": "100.0"})
        assert any("0 days" in n for n in notes)

    def test_low_confidence(self):
        notes = generate_notes([], {"confidence": "low"}, {"inbound": [], "outbound": []}, {"formatted": "100.0"})
        assert any("low confidence" in n.lower() for n in notes)


class TestBuildFlagScope:
    def test_base(self):
        r = build_flag_scope("base", "standard", {"blocks_scanned": 100})
        assert r == {"type": "block_window", "blocksScanned": 100, "approxDays": 90, "depth": "standard"}

    def test_solana(self):
        r = build_flag_scope("solana", "fast", {"sigs_scanned": 50, "tx_parsed": 10})
        assert r["type"] == "signature_window"
        assert r["signaturesScanned"] == 50
        assert r["txParsed"] == 10

    def test_empty_stats_defaults(self):
        assert build_flag_scope("base", "standard", {})["blocksScanned"] == 0

    def test_unknown_chain(self):
        assert build_flag_scope("ethereum", "standard", {})["type"] == "unknown"


class TestParseTransferLogs:
    def test_empty(self):
        assert _parse_transfer_logs([], 18, "in") == []

    def test_missing_data_defaults_0x0(self):
        log = {"blockNumber": "0xa", "topics": ["t0", "0x" + "0" * 64], "transactionHash": "0xabc"}
        assert _parse_transfer_logs([log], 18, "in")[0]["amount"] == "0.0"

    def test_zero_decimals(self):
        log = {"blockNumber": "0x1", "data": "0x" + hex(1000)[2:].zfill(64), "topics": ["t0", "0x" + "a" * 64], "transactionHash": "0x123"}
        assert _parse_transfer_logs([log], 0, "in")[0]["amount"] == "1000.0"

    def test_short_topics(self):
        log = {"blockNumber": "0x5", "data": "0x" + "0" * 64, "topics": ["t0"], "transactionHash": "0x456"}
        assert _parse_transfer_logs([log], 18, "in")[0]["from"] is None

    def test_missing_tx_hash(self):
        log = {"blockNumber": "0x5", "data": "0x" + "0" * 64, "topics": ["t0", "0x" + "a" * 64]}
        assert _parse_transfer_logs([log], 18, "in")[0]["txHash"] == ""

    def test_malformed_block_number_skipped(self):
        log = {"blockNumber": "notahex", "data": "0x" + "0" * 64, "topics": ["t0"], "transactionHash": "0xabc"}
        assert _parse_transfer_logs([log], 18, "in") == []

    def test_max_uint256(self):
        log = {"blockNumber": "0x1", "data": "0x" + "f" * 64, "topics": ["t0", "0x" + "a" * 64], "transactionHash": "0xabc"}
        assert float(_parse_transfer_logs([log], 18, "in")[0]["amount"]) > 1e50

    def test_multiple_logs(self):
        logs = [
            {"blockNumber": hex(i), "data": "0x" + hex(100)[2:].zfill(64), "topics": ["t0", "0x" + "a" * 64], "transactionHash": f"0x{i:064x}"}
            for i in range(1, 4)
        ]
        assert len(_parse_transfer_logs(logs, 18, "in")) == 3

    def test_outbound_has_to(self):
        log = {"blockNumber": "0x1", "data": "0x" + "0" * 64, "topics": ["t0", "0x" + "a" * 64, "0x" + "b" * 64], "transactionHash": "0xabc"}
        entry = _parse_transfer_logs([log], 18, "out")[0]
        assert "to" in entry and "from" not in entry

    def test_inbound_has_from(self):
        log = {"blockNumber": "0x1", "data": "0x" + "0" * 64, "topics": ["t0", "0x" + "a" * 64, "0x" + "b" * 64], "transactionHash": "0xabc"}
        entry = _parse_transfer_logs([log], 18, "in")[0]
        assert "from" in entry and "to" not in entry


class TestFindTokenBalance:
    def test_missing_ui_token_amount(self):
        assert _find_token_balance([{"mint": "abc"}], "acc1", "abc") == 0

    def test_zero(self):
        assert _find_token_balance([{"mint": "abc", "uiTokenAmount": {"amount": "0"}}], "acc1", "abc") == 0

    def test_first_match_wins(self):
        balances = [
            {"mint": "abc", "uiTokenAmount": {"amount": "100"}},
            {"mint": "abc", "uiTokenAmount": {"amount": "200"}},
        ]
        assert _find_token_balance(balances, "acc1", "abc") == 100

    def test_no_match(self):
        assert _find_token_balance([{"mint": "xyz", "uiTokenAmount": {"amount": "100"}}], "acc1", "abc") == 0


class TestDeriveLastTransfers:
    def test_inbound_only(self):
        assert derive_last_transfers({"inbound": [{"tx": "in"}], "outbound": []}) == ({"tx": "in"}, None)

    def test_outbound_only(self):
        assert derive_last_transfers({"inbound": [], "outbound": [{"tx": "out"}]}) == (None, {"tx": "out"})

    def test_first_element(self):
        r = {"inbound": [{"tx": "in1"}, {"tx": "in2"}], "outbound": [{"tx": "out1"}, {"tx": "out2"}]}
        assert derive_last_transfers(r) == ({"tx": "in1"}, {"tx": "out1"})

    def test_empty(self):
        assert derive_last_transfers({"inbound": [], "outbound": []}) == (None, None)


class TestRateLimiterInternals:
    def setup_method(self):
        reset_rate_limits()

    def teardown_method(self):
        reset_rate_limits()

    def test_prune_empty(self):
        _prune("k", time.monotonic(), 60)
        assert _hits["k"] == []

    def test_prune_keeps_within_window(self):
        now = time.monotonic()
        _hits["k"] = [now - 10, now - 5, now - 1]
        _prune("k", now, 60)
        assert len(_hits["k"]) == 3

    def test_is_limited_at_boundary(self):
        now = time.monotonic()
        _hits["k"] = [now - i for i in range(60)]
        assert _is_limited("k", now, 60, 120) is True

    def test_record_appends(self):
        now = time.monotonic()
        _record("k", now)
        _record("k", now + 1)
        assert len(_hits["k"]) == 2

    def test_keys_independent(self):
        now = time.monotonic()
        _hits["a"] = [now] * 100
        _hits["b"] = [now]
        assert _is_limited("a", now, 60, 120) is True
        assert _is_limited("b", now, 60, 120) is False

    def test_zero_window_prunes_all(self):
        now = time.monotonic()
        _hits["k"] = [now - 0.001, now - 0.002]
        _prune("k", now, 0)
        assert _hits["k"] == []


class TestCircuitBreaker:
    def setup_method(self):
        for key in _circuit:
            _circuit[key] = {"open": False, "until": 0}

    def teardown_method(self):
        for key in _circuit:
            _circuit[key] = {"open": False, "until": 0}

    def test_trip_opens(self):
        _trip_circuit("jupiter")
        assert _circuit_open("jupiter") is True

    def test_past_until_auto_resets(self):
        _circuit["jupiter"] = {"open": True, "until": time.time() - 10}
        assert _circuit_open("jupiter") is False
        assert _circuit["jupiter"]["open"] is False

    def test_providers_independent(self):
        _trip_circuit("jupiter")
        assert _circuit_open("jupiter") is True
        assert _circuit_open("dexscreener") is False

    def test_trip_duration(self):
        before = time.time()
        _trip_circuit("dexscreener")
        after = time.time()
        until = _circuit["dexscreener"]["until"]
        assert before + CIRCUIT_OPEN_DURATION <= until <= after + CIRCUIT_OPEN_DURATION

    def test_default_closed(self):
        assert _circuit_open("jupiter") is False


class TestBudgetExceeded:
    def test_at_call_limit(self):
        assert _budget_exceeded(10, 10, time.monotonic(), 5.0) is True

    def test_over_call_limit(self):
        assert _budget_exceeded(11, 10, time.monotonic(), 5.0) is True

    def test_under_call_limit(self):
        assert _budget_exceeded(9, 10, time.monotonic(), 5.0) is False

    def test_over_time_limit(self):
        assert _budget_exceeded(0, 10, time.monotonic() - 6.0, 5.0) is True

    def test_within_time_limit(self):
        assert _budget_exceeded(0, 10, time.monotonic() - 4.0, 5.0) is False


class TestEncodeAddress:
    def test_standard(self):
        assert _encode_address("0x" + ADDR_40) == PADDED_ADDR

    def test_uppercase(self):
        assert _encode_address("0x" + ADDR_40.upper()) == PADDED_ADDR

    def test_no_0x(self):
        assert _encode_address(ADDR_40) == PADDED_ADDR

    def test_64_char_input(self):
        assert _encode_address("0x" + "a" * 64) == "a" * 64


class TestHTTPValidation:
    @pytest.mark.anyio
    async def test_invalid_chain(self, client):
        resp = await client.post("/v1/position-receipt/ethereum", json={"address": "x", "token": "x"})
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_chain"

    @pytest.mark.anyio
    async def test_missing_address(self, client):
        resp = await client.post("/v1/position-receipt/base", json={"token": "0x" + "a" * 40})
        assert resp.status_code == 400
        assert resp.json()["error"] == "missing_address"

    @pytest.mark.anyio
    async def test_missing_token(self, client):
        resp = await client.post("/v1/position-receipt/base", json={"address": "0x" + "a" * 40})
        assert resp.status_code == 400
        assert resp.json()["error"] == "missing_token"

    @pytest.mark.anyio
    async def test_invalid_address(self, client):
        resp = await client.post("/v1/position-receipt/base", json={"address": "bad", "token": "0x" + "a" * 40})
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_address"

    @pytest.mark.anyio
    async def test_invalid_token(self, client):
        resp = await client.post("/v1/position-receipt/base", json={"address": "0x" + "a" * 40, "token": "bad"})
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_token"

    @pytest.mark.anyio
    async def test_invalid_depth(self, client):
        resp = await client.post("/v1/position-receipt/base", json={"address": "0x" + "a" * 40, "token": "0x" + "b" * 40, "depth": "ultra"})
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_depth"

    @pytest.mark.anyio
    async def test_malformed_json(self, client):
        resp = await client.post("/v1/position-receipt/base", content=b"not json", headers={"content-type": "application/json"})
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_body"

    @pytest.mark.anyio
    async def test_string_body(self, client):
        resp = await client.post("/v1/position-receipt/base", content=b'"string"', headers={"content-type": "application/json"})
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_body"

    @pytest.mark.anyio
    async def test_number_body(self, client):
        resp = await client.post("/v1/position-receipt/base", content=b"42", headers={"content-type": "application/json"})
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_body"

    @pytest.mark.anyio
    async def test_null_body(self, client):
        resp = await client.post("/v1/position-receipt/base", content=b"null", headers={"content-type": "application/json"})
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_body"

    @pytest.mark.anyio
    async def test_array_body(self, client):
        resp = await client.post("/v1/position-receipt/base", content=b"[1,2,3]", headers={"content-type": "application/json"})
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_body"

    @pytest.mark.anyio
    async def test_health(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    @pytest.mark.anyio
    async def test_get_info_valid(self, client):
        resp = await client.get("/v1/position-receipt/base")
        assert resp.status_code == 200
        assert resp.json()["chain"] == "base"

    @pytest.mark.anyio
    async def test_get_info_invalid(self, client):
        resp = await client.get("/v1/position-receipt/ethereum")
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_chain"

    @pytest.mark.anyio
    async def test_received_body_truncation(self, client):
        resp = await client.post("/v1/position-receipt/base", json={"address": "0x" + "a" * 300, "token": "0x" + "b" * 40})
        assert resp.status_code == 400
        assert len(resp.json()["received_body"]["address"]) <= 200


class TestHTTPMiddleware:
    @pytest.mark.anyio
    async def test_apix_body_unwrapping(self, client):
        resp = await client.post("/v1/position-receipt/base", json={"body": {"address": "bad", "token": "0x" + "a" * 40}})
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_address"  # found but invalid, not missing

    @pytest.mark.anyio
    async def test_parameter_aliases(self, client):
        resp = await client.post("/v1/position-receipt/base", json={"wallet": "0x" + "a" * 40, "mint": "0x" + "b" * 40})
        assert resp.status_code == 502  # passed all validation, hit RPC

    @pytest.mark.anyio
    async def test_query_fallback(self, client):
        resp = await client.post("/v1/position-receipt/base", json={"query": "0x" + "a" * 40, "token": "0x" + "b" * 40})
        assert resp.status_code == 502  # address extracted from query, passed validation

    @pytest.mark.anyio
    async def test_extra_fields_ignored(self, client):
        resp = await client.post("/v1/position-receipt/base", json={"address": "0x" + "a" * 40, "token": "0x" + "b" * 40, "extra": "x"})
        assert resp.status_code == 502  # extra fields didn't interfere

    @pytest.mark.anyio
    async def test_nested_aliases(self, client):
        resp = await client.post("/v1/position-receipt/base", json={"body": {"wallet": "bad", "mint": "0x" + "a" * 40}})
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_address"  # found via alias in unwrapped body


class TestRateLimiterHTTP:
    @pytest.mark.anyio
    async def test_boundary(self, client):
        now = time.monotonic()
        _hits["ip:127.0.0.1"] = [now - i * 0.5 for i in range(59)]

        # 60th: 59 hits < 60 limit, passes to validation (records hit, total=60)
        resp = await client.post("/v1/position-receipt/base", json={"address": "bad", "token": "bad"})
        assert resp.status_code == 400

        # 61st: 60 hits >= 60 limit, blocked
        resp2 = await client.post("/v1/position-receipt/base", json={"address": "bad", "token": "bad"})
        assert resp2.status_code == 429

    @pytest.mark.anyio
    async def test_get_not_limited(self, client):
        for _ in range(100):
            assert (await client.get("/health")).status_code == 200


class TestHealthReady:
    @pytest.mark.anyio
    async def test_response_shape(self, client):
        resp = await client.get("/health/ready")
        data = resp.json()
        assert resp.status_code in (200, 503)
        assert data["status"] in ("ok", "degraded")
        assert "base_rpc" in data["checks"]
        assert "solana_rpc" in data["checks"]
        for val in data["checks"].values():
            assert val in ("ok", "error", "unreachable")
