"""Tests for mktdata.mktdata.MarketDataServer — feed aggregation, subscriptions, broadcasting."""

import json
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from mktdata.mktdata import MarketDataServer


@pytest.fixture
def mktdata_server():
    """Create a MarketDataServer with mocked WS server and feeds."""
    with patch("mktdata.mktdata.WSServer") as MockServer, \
         patch("mktdata.mktdata.BinanceFeed") as MockBinance, \
         patch("mktdata.mktdata.CoinbaseFeedClass") as MockCoinbase:
        mock_server = MagicMock()
        mock_server.broadcast = AsyncMock()
        mock_server.send_to = AsyncMock()
        mock_server.start = AsyncMock()
        mock_server.stop = AsyncMock()
        mock_server.on_message = MagicMock()
        mock_server.on_connect = MagicMock()
        mock_server.on_disconnect = MagicMock()
        mock_server.clients = set()
        MockServer.return_value = mock_server

        mock_binance = MagicMock()
        mock_binance.start = AsyncMock()
        mock_binance.stop = AsyncMock()
        MockBinance.return_value = mock_binance

        mock_coinbase = MagicMock()
        mock_coinbase.start = AsyncMock()
        mock_coinbase.stop = AsyncMock()
        MockCoinbase.return_value = mock_coinbase

        server = MarketDataServer()
    return server


def _make_market_data(symbol="BTC/USD", exchange="BINANCE", last=67000.0,
                      bid=66999.0, ask=67001.0):
    """Helper to create a market_data dict."""
    return {
        "type": "market_data",
        "symbol": symbol,
        "bid": bid,
        "ask": ask,
        "last": last,
        "bid_size": 1.0,
        "ask_size": 1.0,
        "volume": 1000.0,
        "change_pct": 0.5,
        "exchange": exchange,
        "timestamp": "2026-01-01T00:00:00.000Z",
    }


class TestMarketDataServerConnect:

    @pytest.mark.asyncio
    async def test_new_client_subscribed_to_all(self, mktdata_server):
        """New client should default to subscription=None (all symbols)."""
        ws = AsyncMock()
        await mktdata_server._handle_connect(ws)
        assert ws in mktdata_server._subscriptions
        assert mktdata_server._subscriptions[ws] is None

    @pytest.mark.asyncio
    async def test_connect_sends_cached_snapshots(self, mktdata_server):
        """When a client connects, it should receive all cached market data snapshots."""
        # Pre-populate the cache
        data = _make_market_data("BTC/USD", "BINANCE", 67000.0)
        mktdata_server._latest[("BTC/USD", "BINANCE")] = data

        ws = AsyncMock()
        await mktdata_server._handle_connect(ws)

        ws.send.assert_called_once()
        sent = json.loads(ws.send.call_args[0][0])
        assert sent["symbol"] == "BTC/USD"
        assert sent["exchange"] == "BINANCE"

    @pytest.mark.asyncio
    async def test_connect_snapshot_send_failure_logged(self, mktdata_server):
        """Snapshot send failure should be logged, not crash."""
        data = _make_market_data("BTC/USD", "BINANCE", 67000.0)
        mktdata_server._latest[("BTC/USD", "BINANCE")] = data

        ws = AsyncMock()
        ws.send = AsyncMock(side_effect=Exception("connection lost"))
        # Should not raise
        await mktdata_server._handle_connect(ws)


class TestMarketDataServerDisconnect:

    @pytest.mark.asyncio
    async def test_disconnect_removes_subscription(self, mktdata_server):
        """Disconnecting client should be removed from subscriptions."""
        ws = AsyncMock()
        mktdata_server._subscriptions[ws] = None
        await mktdata_server._handle_disconnect(ws)
        assert ws not in mktdata_server._subscriptions


class TestMarketDataServerSubscribe:

    @pytest.mark.asyncio
    async def test_subscribe_sets_explicit_symbols(self, mktdata_server):
        """Subscribe message should switch from all to explicit symbol set."""
        ws = AsyncMock()
        mktdata_server._subscriptions[ws] = None  # default: all

        msg = json.dumps({"type": "subscribe", "symbols": ["BTC/USD", "ETH/USD"]})
        await mktdata_server._handle_message(ws, msg)

        subs = mktdata_server._subscriptions[ws]
        assert isinstance(subs, set)
        assert "BTC/USD" in subs
        assert "ETH/USD" in subs

    @pytest.mark.asyncio
    async def test_subscribe_adds_to_existing_set(self, mktdata_server):
        """Subscribing when already having explicit symbols should add to the set."""
        ws = AsyncMock()
        mktdata_server._subscriptions[ws] = {"BTC/USD"}

        msg = json.dumps({"type": "subscribe", "symbols": ["ETH/USD"]})
        await mktdata_server._handle_message(ws, msg)

        subs = mktdata_server._subscriptions[ws]
        assert "BTC/USD" in subs
        assert "ETH/USD" in subs

    @pytest.mark.asyncio
    async def test_subscribe_invalid_symbols_ignored(self, mktdata_server):
        """Subscribe with no valid symbols should not change subscriptions."""
        ws = AsyncMock()
        mktdata_server._subscriptions[ws] = None

        msg = json.dumps({"type": "subscribe", "symbols": ["INVALID/PAIR"]})
        await mktdata_server._handle_message(ws, msg)

        # Should remain None (subscribed to all)
        assert mktdata_server._subscriptions[ws] is None


class TestMarketDataServerUnsubscribe:

    @pytest.mark.asyncio
    async def test_unsubscribe_from_all(self, mktdata_server):
        """Unsubscribing when subscribed to all should create set of all minus unsubscribed."""
        ws = AsyncMock()
        mktdata_server._subscriptions[ws] = None

        msg = json.dumps({"type": "unsubscribe", "symbols": ["BTC/USD"]})
        await mktdata_server._handle_message(ws, msg)

        subs = mktdata_server._subscriptions[ws]
        assert isinstance(subs, set)
        assert "BTC/USD" not in subs

    @pytest.mark.asyncio
    async def test_unsubscribe_from_explicit_set(self, mktdata_server):
        """Unsubscribing from an explicit set should remove the symbol."""
        ws = AsyncMock()
        mktdata_server._subscriptions[ws] = {"BTC/USD", "ETH/USD"}

        msg = json.dumps({"type": "unsubscribe", "symbols": ["BTC/USD"]})
        await mktdata_server._handle_message(ws, msg)

        subs = mktdata_server._subscriptions[ws]
        assert "BTC/USD" not in subs
        assert "ETH/USD" in subs


class TestMarketDataServerBroadcast:

    @pytest.mark.asyncio
    async def test_tick_cached_and_broadcast(self, mktdata_server):
        """Market data callback should cache and broadcast to subscribed clients."""
        ws = AsyncMock()
        mktdata_server._subscriptions[ws] = None  # subscribed to all

        data = _make_market_data("BTC/USD", "BINANCE", 67000.0)
        await mktdata_server._on_market_data(data)

        # Check data cached
        assert ("BTC/USD", "BINANCE") in mktdata_server._latest
        assert mktdata_server._latest[("BTC/USD", "BINANCE")] == data

        # Check sent to client
        ws.send.assert_called_once()
        sent = json.loads(ws.send.call_args[0][0])
        assert sent["symbol"] == "BTC/USD"

    @pytest.mark.asyncio
    async def test_tick_not_sent_to_unsubscribed_client(self, mktdata_server):
        """Clients not subscribed to a symbol should not receive its ticks."""
        ws = AsyncMock()
        mktdata_server._subscriptions[ws] = {"ETH/USD"}  # only ETH

        data = _make_market_data("BTC/USD", "BINANCE", 67000.0)
        await mktdata_server._on_market_data(data)

        ws.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_tick_sent_to_subscribed_symbol(self, mktdata_server):
        """Client subscribed to BTC/USD should receive BTC/USD ticks."""
        ws = AsyncMock()
        mktdata_server._subscriptions[ws] = {"BTC/USD"}

        data = _make_market_data("BTC/USD", "BINANCE", 67000.0)
        await mktdata_server._on_market_data(data)

        ws.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_tick_count_increments(self, mktdata_server):
        """Each tick should increment the tick counter."""
        assert mktdata_server._tick_count == 0

        data = _make_market_data("BTC/USD", "BINANCE", 67000.0)
        await mktdata_server._on_market_data(data)
        assert mktdata_server._tick_count == 1

        await mktdata_server._on_market_data(data)
        assert mktdata_server._tick_count == 2

    @pytest.mark.asyncio
    async def test_broadcast_failure_does_not_crash(self, mktdata_server):
        """Send failure during broadcast should not crash the server."""
        ws = AsyncMock()
        ws.send = AsyncMock(side_effect=Exception("broken pipe"))
        mktdata_server._subscriptions[ws] = None

        data = _make_market_data("BTC/USD", "BINANCE", 67000.0)
        # Should not raise
        await mktdata_server._on_market_data(data)


class TestMarketDataServerGetLatest:

    def test_get_latest_with_exchange(self, mktdata_server):
        """get_latest with exchange should return exact match."""
        data = _make_market_data("BTC/USD", "BINANCE", 67000.0)
        mktdata_server._latest[("BTC/USD", "BINANCE")] = data

        result = mktdata_server.get_latest("BTC/USD", "BINANCE")
        assert result == data

    def test_get_latest_without_exchange(self, mktdata_server):
        """get_latest without exchange should return first match for that symbol."""
        data = _make_market_data("ETH/USD", "COINBASE", 3500.0)
        mktdata_server._latest[("ETH/USD", "COINBASE")] = data

        result = mktdata_server.get_latest("ETH/USD")
        assert result == data

    def test_get_latest_missing_returns_none(self, mktdata_server):
        """get_latest for missing symbol should return None."""
        result = mktdata_server.get_latest("DOGE/USD", "BINANCE")
        assert result is None

    def test_get_latest_any_exchange_missing_returns_none(self, mktdata_server):
        """get_latest without exchange for missing symbol should return None."""
        result = mktdata_server.get_latest("DOGE/USD")
        assert result is None


class TestMarketDataServerMessageHandling:

    @pytest.mark.asyncio
    async def test_invalid_json_ignored(self, mktdata_server):
        """Invalid JSON message should be silently handled (logged, not crash)."""
        ws = AsyncMock()
        mktdata_server._subscriptions[ws] = None
        # Should not raise
        await mktdata_server._handle_message(ws, "not valid json{{{")

    @pytest.mark.asyncio
    async def test_unknown_message_type_ignored(self, mktdata_server):
        """Unknown message type should be silently handled."""
        ws = AsyncMock()
        mktdata_server._subscriptions[ws] = None
        msg = json.dumps({"type": "unknown_type"})
        # Should not raise
        await mktdata_server._handle_message(ws, msg)
