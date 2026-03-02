"""Tests for om/order_manager.py — validation, routing, exec report handling."""

import json
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from shared.fix_protocol import (
    FIXMessage, Tag, MsgType, ExecType, OrdStatus, Side, OrdType,
    new_order_single, execution_report,
)
from shared.config import DEFAULT_ROUTING, DEFAULT_RISK_LIMITS, SYMBOLS


@pytest.fixture
def order_manager():
    """Create an OrderManager with mocked WS server and clients."""
    with patch("om.order_manager.WSServer") as MockServer, \
         patch("om.order_manager.WSClient") as MockClient, \
         patch("om.order_manager.risk_limits.load_limits", return_value=dict(DEFAULT_RISK_LIMITS)):
        mock_server = MagicMock()
        mock_server.send_to = AsyncMock()
        mock_server.broadcast = AsyncMock()
        mock_server.start = AsyncMock()
        mock_server.stop = AsyncMock()
        mock_server.on_message = MagicMock()
        mock_server.on_connect = MagicMock()
        mock_server.on_disconnect = MagicMock()
        mock_server.clients = set()
        MockServer.return_value = mock_server

        mock_exchconn = MagicMock()
        mock_exchconn.send = AsyncMock()
        mock_exchconn.connect = AsyncMock()
        mock_exchconn.listen = AsyncMock()
        mock_exchconn.close = AsyncMock()
        mock_exchconn.on_message = MagicMock()
        mock_exchconn._ws = MagicMock()

        mock_pos = MagicMock()
        mock_pos.send = AsyncMock()
        mock_pos.connect = AsyncMock()
        mock_pos.listen = AsyncMock()
        mock_pos.close = AsyncMock()
        mock_pos.on_message = MagicMock()
        mock_pos._ws = MagicMock()

        # WSClient is called twice: first for exchconn, then for pos
        MockClient.side_effect = [mock_exchconn, mock_pos]

        from om.order_manager import OrderManager
        om = OrderManager()
        om._mock_exchconn = mock_exchconn
        om._mock_pos = mock_pos
        yield om


# -----------------------------------------------------------------------
# TestOrderIdGeneration
# -----------------------------------------------------------------------

class TestOrderIdGeneration:

    def test_sequential_ids(self, order_manager):
        id1 = order_manager._next_order_id()
        id2 = order_manager._next_order_id()
        assert id1 == "OM-000001"
        assert id2 == "OM-000002"

    def test_format(self, order_manager):
        oid = order_manager._next_order_id()
        assert oid.startswith("OM-")
        assert len(oid) == 9


# -----------------------------------------------------------------------
# TestResolveExchange
# -----------------------------------------------------------------------

class TestResolveExchange:

    def test_explicit_ex_destination(self, order_manager):
        msg = new_order_single("C1", "BTC/USD", Side.Buy, 1.0, OrdType.Market, exchange="COINBASE")
        assert order_manager._resolve_exchange(msg) == "COINBASE"

    def test_default_routing_btc(self, order_manager):
        msg = new_order_single("C1", "BTC/USD", Side.Buy, 1.0, OrdType.Market)
        assert order_manager._resolve_exchange(msg) == DEFAULT_ROUTING["BTC/USD"]

    def test_default_routing_sol(self, order_manager):
        msg = new_order_single("C1", "SOL/USD", Side.Buy, 1.0, OrdType.Market)
        assert order_manager._resolve_exchange(msg) == DEFAULT_ROUTING["SOL/USD"]


# -----------------------------------------------------------------------
# TestValidateOrder
# -----------------------------------------------------------------------

class TestValidateOrder:

    def test_valid_order_passes(self, order_manager):
        msg = new_order_single("C1", "BTC/USD", Side.Buy, 1.0, OrdType.Limit, 67000.0)
        result = order_manager._validate_order(msg)
        assert result is None

    def test_unknown_symbol_rejected(self, order_manager):
        msg = new_order_single("C1", "XRP/USD", Side.Buy, 1.0, OrdType.Market)
        result = order_manager._validate_order(msg)
        assert result is not None
        assert "Unknown symbol" in result

    def test_zero_qty_rejected(self, order_manager):
        msg = new_order_single("C1", "BTC/USD", Side.Buy, 0.0, OrdType.Market)
        result = order_manager._validate_order(msg)
        assert result is not None
        assert "positive" in result.lower()

    def test_negative_qty_rejected(self, order_manager):
        msg = new_order_single("C1", "BTC/USD", Side.Buy, -1.0, OrdType.Market)
        # qty is set as "-1.0" string, float("-1.0") = -1.0
        result = order_manager._validate_order(msg)
        assert result is not None

    def test_limit_zero_price_rejected(self, order_manager):
        msg = new_order_single("C1", "BTC/USD", Side.Buy, 1.0, OrdType.Limit, 0.0)
        result = order_manager._validate_order(msg)
        assert result is not None
        assert "price" in result.lower()

    def test_risk_breach_rejected(self, order_manager):
        # Exceeds max_order_qty of 10 for BTC/USD (use limit order because
        # market orders hit an UnboundLocalError for `price` in _validate_order)
        msg = new_order_single("C1", "BTC/USD", Side.Buy, 11.0, OrdType.Limit, 5000.0)
        result = order_manager._validate_order(msg)
        assert result is not None
        assert "exceeds" in result.lower()

    def test_market_order_risk_breach_rejected(self, order_manager):
        """Market order with qty exceeding max_order_qty should be rejected."""
        # max_order_qty for BTC/USD is 10.0 in DEFAULT_RISK_LIMITS
        msg = new_order_single("C1", "BTC/USD", Side.Buy, 11.0, OrdType.Market)
        result = order_manager._validate_order(msg)
        assert result is not None
        assert "exceeds" in result.lower()


# -----------------------------------------------------------------------
# TestHandleNewOrder
# -----------------------------------------------------------------------

class TestHandleNewOrder:

    @pytest.mark.asyncio
    async def test_valid_order_accepted(self, order_manager, mock_websocket):
        msg = new_order_single("GUI-1", "BTC/USD", Side.Buy, 1.0, OrdType.Limit, 67000.0)
        await order_manager._handle_new_order(mock_websocket, msg)
        assert "GUI-1" in order_manager.orders
        order = order_manager.orders["GUI-1"]
        assert order["order_id"] == "OM-000001"
        assert order["status"] == OrdStatus.PendingNew

    @pytest.mark.asyncio
    async def test_rejected_order_sends_reject(self, order_manager, mock_websocket):
        msg = new_order_single("GUI-1", "INVALID/USD", Side.Buy, 1.0, OrdType.Market)
        await order_manager._handle_new_order(mock_websocket, msg)
        assert "GUI-1" not in order_manager.orders
        # Should have sent a reject to the websocket
        order_manager.server.send_to.assert_called_once()
        sent_json = order_manager.server.send_to.call_args[0][1]
        sent_msg = FIXMessage.from_json(sent_json)
        assert sent_msg.get(Tag.ExecType) == ExecType.Rejected


# -----------------------------------------------------------------------
# TestHandleExecutionReport
# -----------------------------------------------------------------------

class TestHandleExecutionReport:

    @pytest.mark.asyncio
    async def test_trade_fill_updates_order(self, order_manager, mock_websocket):
        # Setup: create an order in the book
        order_manager.orders["GUI-1"] = {
            "cl_ord_id": "GUI-1",
            "order_id": "OM-000001",
            "symbol": "BTC/USD",
            "side": "1",
            "qty": 1.0,
            "price": 67000.0,
            "ord_type": OrdType.Limit,
            "exchange": "BINANCE",
            "status": OrdStatus.New,
            "cum_qty": 0.0,
            "avg_px": 0.0,
            "leaves_qty": 1.0,
            "source_ws": mock_websocket,
            "created_at": 0,
        }
        order_manager._om_id_to_cl_ord_id["OM-000001"] = "GUI-1"

        report = execution_report(
            cl_ord_id="GUI-1", order_id="OM-000001",
            exec_type=ExecType.Trade, ord_status=OrdStatus.Filled,
            symbol="BTC/USD", side=Side.Buy,
            leaves_qty=0.0, cum_qty=1.0, avg_px=67000.0,
            last_px=67000.0, last_qty=1.0,
        )
        await order_manager._handle_execution_report(report)

        order = order_manager.orders["GUI-1"]
        assert order["cum_qty"] == 1.0
        assert order["leaves_qty"] == 0.0
        assert order["status"] == OrdStatus.Filled

    @pytest.mark.asyncio
    async def test_trade_updates_positions(self, order_manager, mock_websocket):
        order_manager.orders["GUI-1"] = {
            "cl_ord_id": "GUI-1", "order_id": "OM-000001",
            "symbol": "BTC/USD", "side": "1", "qty": 1.0, "price": 67000.0,
            "ord_type": OrdType.Limit, "exchange": "BINANCE",
            "status": OrdStatus.New, "cum_qty": 0.0, "avg_px": 0.0,
            "leaves_qty": 1.0, "source_ws": mock_websocket, "created_at": 0,
        }
        order_manager._om_id_to_cl_ord_id["OM-000001"] = "GUI-1"

        report = execution_report(
            "GUI-1", "OM-000001", ExecType.Trade, OrdStatus.Filled,
            "BTC/USD", Side.Buy, 0.0, 1.0, 67000.0, 67000.0, 1.0,
        )
        await order_manager._handle_execution_report(report)
        assert order_manager._positions["BTC/USD"] == 1.0

    @pytest.mark.asyncio
    async def test_trade_sends_fill_to_posmanager(self, order_manager, mock_websocket):
        order_manager.orders["GUI-1"] = {
            "cl_ord_id": "GUI-1", "order_id": "OM-000001",
            "symbol": "BTC/USD", "side": "1", "qty": 1.0, "price": 67000.0,
            "ord_type": OrdType.Limit, "exchange": "BINANCE",
            "status": OrdStatus.New, "cum_qty": 0.0, "avg_px": 0.0,
            "leaves_qty": 1.0, "source_ws": mock_websocket, "created_at": 0,
        }
        order_manager._om_id_to_cl_ord_id["OM-000001"] = "GUI-1"

        report = execution_report(
            "GUI-1", "OM-000001", ExecType.Trade, OrdStatus.Filled,
            "BTC/USD", Side.Buy, 0.0, 1.0, 67000.0, 67000.0, 1.0,
        )
        await order_manager._handle_execution_report(report)
        order_manager.pos_client.send.assert_called_once()
        fill_json = json.loads(order_manager.pos_client.send.call_args[0][0])
        assert fill_json["type"] == "fill"
        assert fill_json["symbol"] == "BTC/USD"
        assert fill_json["side"] == "BUY"

    @pytest.mark.asyncio
    async def test_cancel_sets_leaves_zero(self, order_manager, mock_websocket):
        order_manager.orders["GUI-1"] = {
            "cl_ord_id": "GUI-1", "order_id": "OM-000001",
            "symbol": "BTC/USD", "side": "1", "qty": 1.0, "price": 67000.0,
            "ord_type": OrdType.Limit, "exchange": "BINANCE",
            "status": OrdStatus.New, "cum_qty": 0.0, "avg_px": 0.0,
            "leaves_qty": 1.0, "source_ws": mock_websocket, "created_at": 0,
        }
        order_manager._om_id_to_cl_ord_id["OM-000001"] = "GUI-1"

        report = execution_report(
            "GUI-1", "OM-000001", ExecType.Canceled, OrdStatus.Canceled,
            "BTC/USD", Side.Buy, 0.0, 0.0, 0.0,
        )
        await order_manager._handle_execution_report(report)
        assert order_manager.orders["GUI-1"]["leaves_qty"] == 0

    @pytest.mark.asyncio
    async def test_replaced_updates_qty_price(self, order_manager, mock_websocket):
        order_manager.orders["GUI-1"] = {
            "cl_ord_id": "GUI-1", "order_id": "OM-000001",
            "symbol": "BTC/USD", "side": "1", "qty": 1.0, "price": 67000.0,
            "ord_type": OrdType.Limit, "exchange": "BINANCE",
            "status": OrdStatus.New, "cum_qty": 0.0, "avg_px": 0.0,
            "leaves_qty": 1.0, "source_ws": mock_websocket, "created_at": 0,
        }
        order_manager._om_id_to_cl_ord_id["OM-000001"] = "GUI-1"

        report = execution_report(
            "GUI-1", "OM-000001", ExecType.Replaced, OrdStatus.Replaced,
            "BTC/USD", Side.Buy, 2.0, 0.0, 0.0,
        )
        report.set(Tag.OrderQty, 2.0)
        report.set(Tag.Price, 68000.0)
        await order_manager._handle_execution_report(report)
        assert order_manager.orders["GUI-1"]["qty"] == 2.0
        assert order_manager.orders["GUI-1"]["price"] == 68000.0

    @pytest.mark.asyncio
    async def test_unknown_order_ignored(self, order_manager):
        report = execution_report(
            "UNKNOWN", "UNKNOWN", ExecType.Trade, OrdStatus.Filled,
            "BTC/USD", Side.Buy, 0.0, 1.0, 67000.0, 67000.0, 1.0,
        )
        # Should not raise
        await order_manager._handle_execution_report(report)

    @pytest.mark.asyncio
    async def test_sell_fill_decrements_position(self, order_manager, mock_websocket):
        order_manager._positions["ETH/USD"] = 10.0
        order_manager.orders["GUI-2"] = {
            "cl_ord_id": "GUI-2", "order_id": "OM-000002",
            "symbol": "ETH/USD", "side": "2", "qty": 5.0, "price": 3500.0,
            "ord_type": OrdType.Limit, "exchange": "BINANCE",
            "status": OrdStatus.New, "cum_qty": 0.0, "avg_px": 0.0,
            "leaves_qty": 5.0, "source_ws": mock_websocket, "created_at": 0,
        }
        order_manager._om_id_to_cl_ord_id["OM-000002"] = "GUI-2"

        report = execution_report(
            "GUI-2", "OM-000002", ExecType.Trade, OrdStatus.Filled,
            "ETH/USD", Side.Sell, 0.0, 5.0, 3500.0, 3500.0, 5.0,
        )
        await order_manager._handle_execution_report(report)
        assert order_manager._positions["ETH/USD"] == 5.0


# -----------------------------------------------------------------------
# TestTerminalOrderCleanup (H-4)
# -----------------------------------------------------------------------

class TestTerminalOrderCleanup:

    @pytest.mark.asyncio
    async def test_filled_order_schedules_cleanup(self, order_manager, mock_websocket):
        """H-4: Terminal orders should schedule cleanup after processing."""
        order_manager.orders["GUI-1"] = {
            "cl_ord_id": "GUI-1", "order_id": "OM-000001",
            "symbol": "BTC/USD", "side": "1", "qty": 1.0, "price": 67000.0,
            "ord_type": OrdType.Limit, "exchange": "BINANCE",
            "status": OrdStatus.New, "cum_qty": 0.0, "avg_px": 0.0,
            "leaves_qty": 1.0, "source_ws": mock_websocket, "created_at": 0,
        }
        order_manager._om_id_to_cl_ord_id["OM-000001"] = "GUI-1"

        report = execution_report(
            "GUI-1", "OM-000001", ExecType.Trade, OrdStatus.Filled,
            "BTC/USD", Side.Buy, 0.0, 1.0, 67000.0, 67000.0, 1.0,
        )
        await order_manager._handle_execution_report(report)
        # Order still exists immediately (cleanup is delayed 60s)
        assert "GUI-1" in order_manager.orders
        # But _cleanup_terminal_order should remove it when called directly
        await order_manager._cleanup_terminal_order("GUI-1", "OM-000001")
        assert "GUI-1" not in order_manager.orders
        assert "OM-000001" not in order_manager._om_id_to_cl_ord_id

    @pytest.mark.asyncio
    async def test_cleanup_skips_non_terminal_order(self, order_manager, mock_websocket):
        """Cleanup should not remove orders that are still active."""
        order_manager.orders["GUI-1"] = {
            "cl_ord_id": "GUI-1", "order_id": "OM-000001",
            "symbol": "BTC/USD", "side": "1", "qty": 1.0, "price": 67000.0,
            "ord_type": OrdType.Limit, "exchange": "BINANCE",
            "status": OrdStatus.New, "cum_qty": 0.0, "avg_px": 0.0,
            "leaves_qty": 1.0, "source_ws": mock_websocket, "created_at": 0,
        }
        order_manager._om_id_to_cl_ord_id["OM-000001"] = "GUI-1"
        # Should not delete because status is New
        await order_manager._cleanup_terminal_order("GUI-1", "OM-000001")
        assert "GUI-1" in order_manager.orders
