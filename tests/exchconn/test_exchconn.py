"""Tests for exchconn/exchconn.py — routing, dispatching, and sanity checks."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.fix_protocol import (
    FIXMessage,
    Tag,
    MsgType,
    ExecType,
    OrdStatus,
    Side,
    OrdType,
    new_order_single,
    cancel_request,
    cancel_replace_request,
)


# ---------------------------------------------------------------------------
# Helper: build an ExchangeConnector with mocked exchanges & server
# ---------------------------------------------------------------------------

def _make_connector():
    """Create an ExchangeConnector with mocked exchange simulators and server."""
    # We need to patch the imports that happen inside ExchangeConnector.__init__
    # so we don't spin up real WSServer / simulators.
    from exchconn.exchconn import ExchangeConnector, MAX_ORDER_QTY

    with patch("exchconn.exchconn.WSServer") as MockWSServer, \
         patch("exchconn.exchconn.BinanceSimulator") as MockBinSim, \
         patch("exchconn.exchconn.CoinbaseExchange") as MockCBExch, \
         patch("exchconn.exchconn.KrakenSimulator") as MockKrkSim, \
         patch("exchconn.exchconn.BybitSimulator") as MockBybSim, \
         patch("exchconn.exchconn.OKXSimulator") as MockOkxSim, \
         patch("exchconn.exchconn.BitfinexSimulator") as MockBfxSim, \
         patch("exchconn.exchconn.HTXSimulator") as MockHtxSim:

        mock_server = MagicMock()
        mock_server.send_to = AsyncMock()
        mock_server.broadcast = AsyncMock()
        mock_server.start = AsyncMock()
        mock_server.stop = AsyncMock()
        MockWSServer.return_value = mock_server

        mock_binance = AsyncMock()
        mock_binance.name = "BINANCE"
        MockBinSim.return_value = mock_binance

        mock_coinbase = AsyncMock()
        mock_coinbase.name = "COINBASE"
        MockCBExch.return_value = mock_coinbase

        mock_kraken = AsyncMock()
        mock_kraken.name = "KRAKEN"
        MockKrkSim.return_value = mock_kraken

        mock_bybit = AsyncMock()
        mock_bybit.name = "BYBIT"
        MockBybSim.return_value = mock_bybit

        mock_okx = AsyncMock()
        mock_okx.name = "OKX"
        MockOkxSim.return_value = mock_okx

        mock_bitfinex = AsyncMock()
        mock_bitfinex.name = "BITFINEX"
        MockBfxSim.return_value = mock_bitfinex

        mock_htx = AsyncMock()
        mock_htx.name = "HTX"
        MockHtxSim.return_value = mock_htx

        connector = ExchangeConnector()
        # Replace the exchanges dict to ensure we're using our mocks
        connector._exchanges = {
            "BINANCE": mock_binance,
            "COINBASE": mock_coinbase,
            "KRAKEN": mock_kraken,
            "BYBIT": mock_bybit,
            "OKX": mock_okx,
            "BITFINEX": mock_bitfinex,
            "HTX": mock_htx,
        }
        connector._server = mock_server

    return connector, mock_binance, mock_coinbase, mock_server, mock_kraken, mock_bybit, mock_okx, mock_bitfinex, mock_htx


@pytest.fixture
def connector_env():
    """Fixture returning (connector, binance_mock, coinbase_mock, server_mock, kraken_mock, bybit_mock, okx_mock, bitfinex_mock, htx_mock)."""
    return _make_connector()


# ---------------------------------------------------------------------------
# Routing tests: ExDestination tag
# ---------------------------------------------------------------------------

class TestExDestinationRouting:

    @pytest.mark.asyncio
    async def test_routes_to_binance_when_specified(self, connector_env):
        connector, binance, coinbase, server, *_ = connector_env
        msg = new_order_single("C1", "BTC/USD", Side.Buy, 1.0, OrdType.Limit, 67000.0, exchange="BINANCE")
        raw = msg.to_json()

        ws = AsyncMock()
        await connector._handle_message(ws, raw)

        binance.submit_order.assert_called_once()
        coinbase.submit_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_routes_to_coinbase_when_specified(self, connector_env):
        connector, binance, coinbase, server, *_ = connector_env
        msg = new_order_single("C1", "SOL/USD", Side.Buy, 10.0, OrdType.Limit, 178.0, exchange="COINBASE")
        raw = msg.to_json()

        ws = AsyncMock()
        await connector._handle_message(ws, raw)

        coinbase.submit_order.assert_called_once()
        binance.submit_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_unknown_exchange(self, connector_env):
        connector, binance, coinbase, server, *_ = connector_env
        msg = new_order_single("C1", "BTC/USD", Side.Buy, 1.0, OrdType.Limit, 67000.0, exchange="NONEXISTENT")
        raw = msg.to_json()

        ws = AsyncMock()
        connector._om_clients.add(ws)
        await connector._handle_message(ws, raw)

        binance.submit_order.assert_not_called()
        coinbase.submit_order.assert_not_called()
        # A reject should be sent
        server.send_to.assert_called_once()
        sent_json = server.send_to.call_args[0][1]
        sent = FIXMessage.from_json(sent_json)
        assert sent.get(Tag.ExecType) == ExecType.Rejected
        assert "NONEXISTENT" in sent.get(Tag.Text)


# ---------------------------------------------------------------------------
# Routing tests: Default routing fallback
# ---------------------------------------------------------------------------

class TestDefaultRouting:

    @pytest.mark.asyncio
    async def test_btc_defaults_to_binance(self, connector_env):
        connector, binance, coinbase, server, *_ = connector_env
        msg = new_order_single("C1", "BTC/USD", Side.Buy, 1.0, OrdType.Limit, 67000.0)
        # No ExDestination set
        raw = msg.to_json()

        ws = AsyncMock()
        await connector._handle_message(ws, raw)

        binance.submit_order.assert_called_once()
        coinbase.submit_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_sol_defaults_to_coinbase(self, connector_env):
        connector, binance, coinbase, server, *_ = connector_env
        msg = new_order_single("C1", "SOL/USD", Side.Buy, 10.0, OrdType.Limit, 178.0)
        raw = msg.to_json()

        ws = AsyncMock()
        await connector._handle_message(ws, raw)

        coinbase.submit_order.assert_called_once()
        binance.submit_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_doge_defaults_to_bybit(self, connector_env):
        connector, binance, coinbase, server, kraken, bybit, *_ = connector_env
        msg = new_order_single("C1", "DOGE/USD", Side.Buy, 100.0, OrdType.Market)
        raw = msg.to_json()

        ws = AsyncMock()
        await connector._handle_message(ws, raw)

        bybit.submit_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_symbol_defaults_to_binance(self, connector_env):
        connector, binance, coinbase, server, *_ = connector_env
        msg = new_order_single("C1", "XRP/USD", Side.Buy, 100.0, OrdType.Market)
        raw = msg.to_json()

        ws = AsyncMock()
        await connector._handle_message(ws, raw)

        # DEFAULT_ROUTING.get("XRP/USD", "BINANCE") -> BINANCE
        binance.submit_order.assert_called_once()


# ---------------------------------------------------------------------------
# Message type dispatching
# ---------------------------------------------------------------------------

class TestMessageDispatching:

    @pytest.mark.asyncio
    async def test_new_order_single_dispatches_submit(self, connector_env):
        connector, binance, coinbase, server, *_ = connector_env
        msg = new_order_single("C1", "BTC/USD", Side.Buy, 1.0, OrdType.Limit, 67000.0, exchange="BINANCE")
        raw = msg.to_json()

        ws = AsyncMock()
        await connector._handle_message(ws, raw)

        binance.submit_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_request_dispatches_cancel(self, connector_env):
        connector, binance, coinbase, server, *_ = connector_env
        msg = cancel_request("CXL-1", "C1", "BTC/USD", Side.Buy)
        msg.set(Tag.ExDestination, "BINANCE")
        raw = msg.to_json()

        ws = AsyncMock()
        await connector._handle_message(ws, raw)

        binance.cancel_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_replace_dispatches_amend(self, connector_env):
        connector, binance, coinbase, server, *_ = connector_env
        msg = cancel_replace_request("AMD-1", "C1", "BTC/USD", Side.Buy, 2.0, 68000.0)
        msg.set(Tag.ExDestination, "BINANCE")
        raw = msg.to_json()

        ws = AsyncMock()
        await connector._handle_message(ws, raw)

        binance.amend_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_heartbeat_is_ignored(self, connector_env):
        connector, binance, coinbase, server, *_ = connector_env
        msg = FIXMessage(MsgType.Heartbeat)
        raw = msg.to_json()

        ws = AsyncMock()
        await connector._handle_message(ws, raw)

        binance.submit_order.assert_not_called()
        coinbase.submit_order.assert_not_called()


# ---------------------------------------------------------------------------
# Sanity check (Task #3)
# ---------------------------------------------------------------------------

class TestSanityCheck:

    @pytest.mark.asyncio
    async def test_rejects_zero_qty(self, connector_env):
        connector, binance, coinbase, server, *_ = connector_env
        msg = new_order_single("C1", "BTC/USD", Side.Buy, 0.0, OrdType.Market)
        msg.set(Tag.ExDestination, "BINANCE")
        raw = msg.to_json()

        ws = AsyncMock()
        connector._om_clients.add(ws)
        await connector._handle_message(ws, raw)

        binance.submit_order.assert_not_called()
        server.send_to.assert_called_once()
        sent = FIXMessage.from_json(server.send_to.call_args[0][1])
        assert sent.get(Tag.ExecType) == ExecType.Rejected
        assert "Invalid quantity" in sent.get(Tag.Text)

    @pytest.mark.asyncio
    async def test_rejects_negative_qty(self, connector_env):
        connector, binance, coinbase, server, *_ = connector_env
        msg = new_order_single("C1", "BTC/USD", Side.Buy, 1.0, OrdType.Market)
        msg.set(Tag.OrderQty, "-5")
        msg.set(Tag.ExDestination, "BINANCE")
        raw = msg.to_json()

        ws = AsyncMock()
        connector._om_clients.add(ws)
        await connector._handle_message(ws, raw)

        binance.submit_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_qty_above_safety_limit(self, connector_env):
        connector, binance, coinbase, server, *_ = connector_env
        from exchconn.exchconn import MAX_ORDER_QTY
        msg = new_order_single("C1", "BTC/USD", Side.Buy, MAX_ORDER_QTY + 1, OrdType.Market)
        msg.set(Tag.ExDestination, "BINANCE")
        raw = msg.to_json()

        ws = AsyncMock()
        connector._om_clients.add(ws)
        await connector._handle_message(ws, raw)

        binance.submit_order.assert_not_called()
        server.send_to.assert_called_once()
        sent = FIXMessage.from_json(server.send_to.call_args[0][1])
        assert sent.get(Tag.ExecType) == ExecType.Rejected
        assert "safety limit" in sent.get(Tag.Text)

    @pytest.mark.asyncio
    async def test_allows_valid_qty(self, connector_env):
        connector, binance, coinbase, server, *_ = connector_env
        msg = new_order_single("C1", "BTC/USD", Side.Buy, 5.0, OrdType.Limit, 67000.0, exchange="BINANCE")
        raw = msg.to_json()

        ws = AsyncMock()
        await connector._handle_message(ws, raw)

        binance.submit_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_allows_max_qty(self, connector_env):
        connector, binance, coinbase, server, *_ = connector_env
        from exchconn.exchconn import MAX_ORDER_QTY
        msg = new_order_single("C1", "BTC/USD", Side.Buy, float(MAX_ORDER_QTY), OrdType.Limit, 67000.0, exchange="BINANCE")
        raw = msg.to_json()

        ws = AsyncMock()
        await connector._handle_message(ws, raw)

        binance.submit_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_allows_large_ada_doge_qty(self, connector_env):
        """H-5: MAX_ORDER_QTY should be high enough for ADA/DOGE coins."""
        connector, binance, coinbase, server, *_ = connector_env
        msg = new_order_single("C1", "ADA/USD", Side.Buy, 50000.0, OrdType.Limit, 0.72, exchange="COINBASE")
        raw = msg.to_json()

        ws = AsyncMock()
        await connector._handle_message(ws, raw)

        coinbase.submit_order.assert_called_once()


# ---------------------------------------------------------------------------
# Amend sanity check (H-5)
# ---------------------------------------------------------------------------

class TestAmendSanityCheck:

    @pytest.mark.asyncio
    async def test_amend_rejects_zero_qty(self, connector_env):
        connector, binance, coinbase, server, *_ = connector_env
        msg = cancel_replace_request("AMD-1", "C1", "BTC/USD", Side.Buy, 0.0, 68000.0)
        msg.set(Tag.ExDestination, "BINANCE")
        raw = msg.to_json()

        ws = AsyncMock()
        connector._om_clients.add(ws)
        await connector._handle_message(ws, raw)

        binance.amend_order.assert_not_called()
        server.send_to.assert_called_once()
        sent = FIXMessage.from_json(server.send_to.call_args[0][1])
        assert sent.get(Tag.ExecType) == ExecType.Rejected
        assert "Invalid amend quantity" in sent.get(Tag.Text)

    @pytest.mark.asyncio
    async def test_amend_rejects_negative_qty(self, connector_env):
        connector, binance, coinbase, server, *_ = connector_env
        msg = cancel_replace_request("AMD-1", "C1", "BTC/USD", Side.Buy, 5.0, 68000.0)
        msg.set(Tag.OrderQty, "-5")
        msg.set(Tag.ExDestination, "BINANCE")
        raw = msg.to_json()

        ws = AsyncMock()
        connector._om_clients.add(ws)
        await connector._handle_message(ws, raw)

        binance.amend_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_amend_rejects_qty_above_safety_limit(self, connector_env):
        connector, binance, coinbase, server, *_ = connector_env
        from exchconn.exchconn import MAX_ORDER_QTY
        msg = cancel_replace_request("AMD-1", "C1", "BTC/USD", Side.Buy, MAX_ORDER_QTY + 1, 68000.0)
        msg.set(Tag.ExDestination, "BINANCE")
        raw = msg.to_json()

        ws = AsyncMock()
        connector._om_clients.add(ws)
        await connector._handle_message(ws, raw)

        binance.amend_order.assert_not_called()
        server.send_to.assert_called_once()
        sent = FIXMessage.from_json(server.send_to.call_args[0][1])
        assert sent.get(Tag.ExecType) == ExecType.Rejected
        assert "safety limit" in sent.get(Tag.Text)

    @pytest.mark.asyncio
    async def test_amend_allows_valid_qty(self, connector_env):
        connector, binance, coinbase, server, *_ = connector_env
        msg = cancel_replace_request("AMD-1", "C1", "BTC/USD", Side.Buy, 2.0, 68000.0)
        msg.set(Tag.ExDestination, "BINANCE")
        raw = msg.to_json()

        ws = AsyncMock()
        await connector._handle_message(ws, raw)

        binance.amend_order.assert_called_once()
