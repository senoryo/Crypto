"""Tests for shared/ws_transport.py — PubSub, json_msg, parse_json_msg."""

import json

import pytest

from shared.ws_transport import PubSub, json_msg, parse_json_msg


# -----------------------------------------------------------------------
# TestJsonMsg
# -----------------------------------------------------------------------

class TestJsonMsg:

    def test_basic_message(self):
        result = json_msg("market_data", symbol="BTC/USD", price=67000.0)
        parsed = json.loads(result)
        assert parsed["type"] == "market_data"
        assert parsed["symbol"] == "BTC/USD"
        assert parsed["price"] == 67000.0

    def test_no_kwargs(self):
        result = json_msg("heartbeat")
        parsed = json.loads(result)
        assert parsed == {"type": "heartbeat"}


# -----------------------------------------------------------------------
# TestParseJsonMsg
# -----------------------------------------------------------------------

class TestParseJsonMsg:

    def test_valid_parse(self):
        raw = '{"type": "fill", "qty": 1.0}'
        parsed = parse_json_msg(raw)
        assert parsed["type"] == "fill"
        assert parsed["qty"] == 1.0

    def test_invalid_raises(self):
        with pytest.raises(json.JSONDecodeError):
            parse_json_msg("not json")


# -----------------------------------------------------------------------
# TestPubSub
# -----------------------------------------------------------------------

class TestPubSub:

    @pytest.mark.asyncio
    async def test_subscribe_and_publish(self):
        ps = PubSub()
        received = []

        async def handler(msg):
            received.append(msg)

        ps.subscribe("tick", handler)
        await ps.publish("tick", {"price": 100})
        assert len(received) == 1
        assert received[0]["price"] == 100

    @pytest.mark.asyncio
    async def test_nonexistent_topic(self):
        ps = PubSub()
        # Should not raise
        await ps.publish("unknown", "data")

    @pytest.mark.asyncio
    async def test_unsubscribe(self):
        ps = PubSub()
        received = []

        async def handler(msg):
            received.append(msg)

        ps.subscribe("tick", handler)
        ps.unsubscribe("tick", handler)
        await ps.publish("tick", "data")
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self):
        ps = PubSub()
        results_a = []
        results_b = []

        async def handler_a(msg):
            results_a.append(msg)

        async def handler_b(msg):
            results_b.append(msg)

        ps.subscribe("tick", handler_a)
        ps.subscribe("tick", handler_b)
        await ps.publish("tick", "hello")
        assert len(results_a) == 1
        assert len(results_b) == 1
