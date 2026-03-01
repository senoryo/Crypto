"""Tests for shared/fix_protocol.py — FIXMessage encode/decode, factories."""

import json

from shared.fix_protocol import (
    FIXMessage,
    Tag,
    MsgType,
    ExecType,
    OrdStatus,
    Side,
    OrdType,
    new_order_single,
    execution_report,
    cancel_request,
    cancel_replace_request,
)


# -----------------------------------------------------------------------
# TestFIXMessageConstruction
# -----------------------------------------------------------------------

class TestFIXMessageConstruction:

    def test_empty_message_has_begin_string(self):
        msg = FIXMessage()
        assert msg.get(Tag.BeginString) == "FIX.4.4"

    def test_msg_type_set_from_constructor(self):
        msg = FIXMessage(MsgType.NewOrderSingle)
        assert msg.msg_type == "D"

    def test_fields_dict_populated(self):
        msg = FIXMessage(MsgType.Heartbeat)
        assert Tag.BeginString in msg.fields
        assert Tag.MsgType in msg.fields

    def test_set_returns_self_for_chaining(self):
        msg = FIXMessage()
        result = msg.set(Tag.Symbol, "BTC/USD")
        assert result is msg

    def test_chained_set_calls(self):
        msg = FIXMessage().set(Tag.Symbol, "BTC/USD").set(Tag.Side, "1")
        assert msg.get(Tag.Symbol) == "BTC/USD"
        assert msg.get(Tag.Side) == "1"

    def test_get_default_for_missing_tag(self):
        msg = FIXMessage()
        assert msg.get(Tag.Symbol) == ""
        assert msg.get(Tag.Symbol, "N/A") == "N/A"

    def test_set_coerces_to_string(self):
        msg = FIXMessage()
        msg.set(Tag.OrderQty, 5.5)
        assert msg.get(Tag.OrderQty) == "5.5"
        msg.set(Tag.Price, 100)
        assert msg.get(Tag.Price) == "100"

    def test_auto_transact_time(self):
        msg = FIXMessage()
        tt = msg.get(Tag.TransactTime)
        assert tt != ""
        assert "-" in tt  # format: YYYYMMDD-HH:MM:SS

    def test_fields_kwarg_overrides(self):
        msg = FIXMessage(fields={Tag.Symbol: "ETH/USD", Tag.Side: "2"})
        assert msg.get(Tag.Symbol) == "ETH/USD"
        assert msg.get(Tag.Side) == "2"


# -----------------------------------------------------------------------
# TestFIXMessageEncodeDecode
# -----------------------------------------------------------------------

class TestFIXMessageEncodeDecode:

    def test_encode_contains_tags(self):
        msg = FIXMessage(MsgType.NewOrderSingle)
        msg.set(Tag.Symbol, "BTC/USD")
        encoded = msg.encode()
        assert "55=BTC/USD" in encoded
        assert "35=D" in encoded

    def test_checksum_three_digit_format(self):
        msg = FIXMessage(MsgType.Heartbeat)
        encoded = msg.encode()
        # Last field should be 10=NNN
        parts = encoded.split("|")
        checksum_part = parts[-1]
        assert checksum_part.startswith("10=")
        assert len(checksum_part.split("=")[1]) == 3

    def test_checksum_validity(self):
        msg = FIXMessage(MsgType.NewOrderSingle)
        msg.set(Tag.Symbol, "BTC/USD")
        encoded = msg.encode()
        parts = encoded.split("|")
        body = "|".join(parts[:-1])
        expected_cs = sum(ord(c) for c in body) % 256
        actual_cs = int(parts[-1].split("=")[1])
        assert actual_cs == expected_cs

    def test_decode_roundtrip(self):
        original = FIXMessage(MsgType.NewOrderSingle)
        original.set(Tag.ClOrdID, "TEST-1")
        original.set(Tag.Symbol, "ETH/USD")
        original.set(Tag.Side, Side.Buy)
        original.set(Tag.OrderQty, 10.0)
        encoded = original.encode()
        decoded = FIXMessage.decode(encoded)
        assert decoded.msg_type == MsgType.NewOrderSingle
        assert decoded.get(Tag.ClOrdID) == "TEST-1"
        assert decoded.get(Tag.Symbol) == "ETH/USD"
        assert decoded.get(Tag.Side) == Side.Buy

    def test_equals_in_value_preserved(self):
        msg = FIXMessage()
        msg.set(Tag.Text, "a=b=c")
        encoded = msg.encode()
        decoded = FIXMessage.decode(encoded)
        assert decoded.get(Tag.Text) == "a=b=c"


# -----------------------------------------------------------------------
# TestFIXMessageJSON
# -----------------------------------------------------------------------

class TestFIXMessageJSON:

    def test_to_json_format(self):
        msg = FIXMessage(MsgType.Heartbeat)
        j = msg.to_json()
        parsed = json.loads(j)
        assert "fix" in parsed
        assert parsed["fix"][Tag.MsgType] == "0"

    def test_from_json_roundtrip(self):
        original = FIXMessage(MsgType.NewOrderSingle)
        original.set(Tag.Symbol, "SOL/USD")
        original.set(Tag.Price, 178.0)
        j = original.to_json()
        restored = FIXMessage.from_json(j)
        assert restored.msg_type == MsgType.NewOrderSingle
        assert restored.get(Tag.Symbol) == "SOL/USD"
        assert restored.get(Tag.Price) == "178.0"


# -----------------------------------------------------------------------
# TestFactoryFunctions
# -----------------------------------------------------------------------

class TestFactoryFunctions:

    def test_new_order_single_limit(self):
        msg = new_order_single("C1", "BTC/USD", Side.Buy, 2.0, OrdType.Limit, 65000.0, "BINANCE")
        assert msg.msg_type == MsgType.NewOrderSingle
        assert msg.get(Tag.ClOrdID) == "C1"
        assert msg.get(Tag.Symbol) == "BTC/USD"
        assert msg.get(Tag.Side) == Side.Buy
        assert msg.get(Tag.OrderQty) == "2.0"
        assert msg.get(Tag.OrdType) == OrdType.Limit
        assert msg.get(Tag.Price) == "65000.0"
        assert msg.get(Tag.ExDestination) == "BINANCE"

    def test_new_order_single_market_no_price(self):
        msg = new_order_single("C2", "ETH/USD", Side.Sell, 5.0, OrdType.Market)
        assert msg.msg_type == MsgType.NewOrderSingle
        assert msg.get(Tag.OrdType) == OrdType.Market
        # Market orders should not have Price set
        assert msg.get(Tag.Price) == ""
        # No exchange specified
        assert msg.get(Tag.ExDestination) == ""

    def test_execution_report_trade(self):
        msg = execution_report(
            "C1", "OM-1", ExecType.Trade, OrdStatus.Filled,
            "BTC/USD", Side.Buy, 0.0, 1.0, 67000.0, 67000.0, 1.0,
        )
        assert msg.msg_type == MsgType.ExecutionReport
        assert msg.get(Tag.ExecType) == ExecType.Trade
        assert msg.get(Tag.OrdStatus) == OrdStatus.Filled
        assert msg.get(Tag.LastPx) == "67000.0"
        assert msg.get(Tag.LastQty) == "1.0"

    def test_execution_report_reject(self):
        msg = execution_report(
            "C1", "NONE", ExecType.Rejected, OrdStatus.Rejected,
            "BTC/USD", Side.Buy, 0, 0, 0, text="Risk breach",
        )
        assert msg.get(Tag.ExecType) == ExecType.Rejected
        assert msg.get(Tag.Text) == "Risk breach"
        # last_px=0 should not be set
        assert msg.get(Tag.LastPx) == ""

    def test_cancel_request(self):
        msg = cancel_request("CXL-1", "C1", "BTC/USD", Side.Buy)
        assert msg.msg_type == MsgType.OrderCancelRequest
        assert msg.get(Tag.ClOrdID) == "CXL-1"
        assert msg.get(Tag.OrigClOrdID) == "C1"
        assert msg.get(Tag.Symbol) == "BTC/USD"

    def test_cancel_replace_request(self):
        msg = cancel_replace_request("AMD-1", "C1", "BTC/USD", Side.Buy, 3.0, 68000.0)
        assert msg.msg_type == MsgType.OrderCancelReplaceRequest
        assert msg.get(Tag.ClOrdID) == "AMD-1"
        assert msg.get(Tag.OrigClOrdID) == "C1"
        assert msg.get(Tag.OrderQty) == "3.0"
        assert msg.get(Tag.Price) == "68000.0"
