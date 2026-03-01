"""Tests for guibroker/guibroker.py — JSON/FIX bridging, cancel/amend ID mapping."""

import json
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from shared.fix_protocol import (
    FIXMessage, Tag, MsgType, ExecType, OrdStatus, Side, OrdType,
    execution_report,
)


@pytest.fixture
def gui_broker():
    """Create a GUIBroker with mocked WS server and client."""
    with patch("guibroker.guibroker.WSServer") as MockServer, \
         patch("guibroker.guibroker.WSClient") as MockClient:
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

        mock_om = MagicMock()
        mock_om.send = AsyncMock()
        mock_om.connect = AsyncMock()
        mock_om.listen = AsyncMock()
        mock_om.close = AsyncMock()
        mock_om.on_message = MagicMock()
        mock_om._ws = MagicMock()
        MockClient.return_value = mock_om

        from guibroker.guibroker import GUIBroker
        broker = GUIBroker()

    broker._mock_om = mock_om
    # Simulate OM connected so messages go through
    broker._om_connected = True
    return broker


# -----------------------------------------------------------------------
# TestClOrdIdGeneration
# -----------------------------------------------------------------------

class TestClOrdIdGeneration:

    def test_sequential_ids(self, gui_broker):
        id1 = gui_broker._next_cl_ord_id()
        id2 = gui_broker._next_cl_ord_id()
        assert id1 == "GUI-1"
        assert id2 == "GUI-2"


# -----------------------------------------------------------------------
# TestMappingConstants
# -----------------------------------------------------------------------

class TestMappingConstants:

    def test_side_map_coverage(self):
        from guibroker.guibroker import SIDE_MAP
        assert SIDE_MAP["BUY"] == Side.Buy
        assert SIDE_MAP["SELL"] == Side.Sell

    def test_ord_type_map_coverage(self):
        from guibroker.guibroker import ORD_TYPE_MAP
        assert ORD_TYPE_MAP["MARKET"] == OrdType.Market
        assert ORD_TYPE_MAP["LIMIT"] == OrdType.Limit

    def test_exec_type_reverse_coverage(self):
        from guibroker.guibroker import EXEC_TYPE_REVERSE
        assert EXEC_TYPE_REVERSE[ExecType.New] == "NEW"
        assert EXEC_TYPE_REVERSE[ExecType.Trade] == "TRADE"
        assert EXEC_TYPE_REVERSE[ExecType.Canceled] == "CANCELED"
        assert EXEC_TYPE_REVERSE[ExecType.Replaced] == "REPLACED"
        assert EXEC_TYPE_REVERSE[ExecType.Rejected] == "REJECTED"
        assert EXEC_TYPE_REVERSE[ExecType.PendingNew] == "PENDING_NEW"


# -----------------------------------------------------------------------
# TestHandleNewOrder
# -----------------------------------------------------------------------

class TestHandleNewOrder:

    @pytest.mark.asyncio
    async def test_assigns_cl_ord_id(self, gui_broker, mock_websocket):
        msg = {"type": "new_order", "symbol": "BTC/USD", "side": "BUY",
               "qty": 1.0, "ord_type": "LIMIT", "price": 67000.0, "exchange": "BINANCE"}
        await gui_broker._handle_new_order(mock_websocket, msg)
        assert "GUI-1" in gui_broker._client_orders
        assert gui_broker._client_orders["GUI-1"] is mock_websocket

    @pytest.mark.asyncio
    async def test_sends_ack_to_gui(self, gui_broker, mock_websocket):
        msg = {"type": "new_order", "symbol": "BTC/USD", "side": "BUY",
               "qty": 1.0, "ord_type": "LIMIT", "price": 67000.0, "exchange": "BINANCE"}
        await gui_broker._handle_new_order(mock_websocket, msg)
        # First call to send_to should be the ack
        ack_json = gui_broker._gui_server.send_to.call_args_list[0][0][1]
        ack = json.loads(ack_json)
        assert ack["type"] == "order_ack"
        assert ack["cl_ord_id"] == "GUI-1"
        assert ack["symbol"] == "BTC/USD"

    @pytest.mark.asyncio
    async def test_auto_exchange_cleared(self, gui_broker, mock_websocket):
        msg = {"type": "new_order", "symbol": "SOL/USD", "side": "SELL",
               "qty": 10.0, "ord_type": "MARKET", "price": 0, "exchange": "AUTO"}
        await gui_broker._handle_new_order(mock_websocket, msg)
        # The FIX message sent to OM should not have ExDestination=AUTO
        sent_json = gui_broker._mock_om.send.call_args[0][0]
        fix_msg = FIXMessage.from_json(sent_json)
        assert fix_msg.get(Tag.ExDestination) == ""

    @pytest.mark.asyncio
    async def test_sends_fix_to_om(self, gui_broker, mock_websocket):
        msg = {"type": "new_order", "symbol": "ETH/USD", "side": "BUY",
               "qty": 5.0, "ord_type": "LIMIT", "price": 3400.0, "exchange": "BINANCE"}
        await gui_broker._handle_new_order(mock_websocket, msg)
        gui_broker._mock_om.send.assert_called_once()
        sent_json = gui_broker._mock_om.send.call_args[0][0]
        fix_msg = FIXMessage.from_json(sent_json)
        assert fix_msg.msg_type == MsgType.NewOrderSingle
        assert fix_msg.get(Tag.Symbol) == "ETH/USD"


# -----------------------------------------------------------------------
# TestHandleCancelOrder
# -----------------------------------------------------------------------

class TestHandleCancelOrder:

    @pytest.mark.asyncio
    async def test_maps_cancel_to_original(self, gui_broker, mock_websocket):
        msg = {"type": "cancel_order", "cl_ord_id": "GUI-1",
               "symbol": "BTC/USD", "side": "BUY"}
        await gui_broker._handle_cancel_order(mock_websocket, msg)
        # New cancel ID should be GUI-1 (first assigned), mapping to orig GUI-1
        cancel_id = "GUI-1"  # next_cl_ord_id returns GUI-1
        assert gui_broker._cancel_to_orig[cancel_id] == "GUI-1"

    @pytest.mark.asyncio
    async def test_sends_fix_cancel_to_om(self, gui_broker, mock_websocket):
        msg = {"type": "cancel_order", "cl_ord_id": "GUI-1",
               "symbol": "BTC/USD", "side": "BUY"}
        await gui_broker._handle_cancel_order(mock_websocket, msg)
        gui_broker._mock_om.send.assert_called_once()
        sent_json = gui_broker._mock_om.send.call_args[0][0]
        fix_msg = FIXMessage.from_json(sent_json)
        assert fix_msg.msg_type == MsgType.OrderCancelRequest
        assert fix_msg.get(Tag.OrigClOrdID) == "GUI-1"


# -----------------------------------------------------------------------
# TestHandleExecutionReport
# -----------------------------------------------------------------------

class TestHandleExecutionReport:

    @pytest.mark.asyncio
    async def test_fix_to_json_conversion(self, gui_broker, mock_websocket):
        gui_broker._client_orders["GUI-1"] = mock_websocket
        report = execution_report(
            "GUI-1", "OM-000001", ExecType.Trade, OrdStatus.Filled,
            "BTC/USD", Side.Buy, 0.0, 1.0, 67000.0, 67000.0, 1.0,
        )
        await gui_broker._handle_execution_report(report)
        gui_broker._gui_server.send_to.assert_called_once()
        sent_json = json.loads(gui_broker._gui_server.send_to.call_args[0][1])
        assert sent_json["type"] == "execution_report"
        assert sent_json["exec_type"] == "TRADE"
        assert sent_json["status"] == "FILLED"
        assert sent_json["side"] == "BUY"
        assert sent_json["symbol"] == "BTC/USD"
        assert sent_json["cl_ord_id"] == "GUI-1"

    @pytest.mark.asyncio
    async def test_cancel_response_uses_original_cl_ord_id(self, gui_broker, mock_websocket):
        gui_broker._client_orders["GUI-2"] = mock_websocket
        gui_broker._cancel_to_orig["GUI-2"] = "GUI-1"
        report = execution_report(
            "GUI-2", "OM-000001", ExecType.Canceled, OrdStatus.Canceled,
            "BTC/USD", Side.Buy, 0.0, 0.0, 0.0,
        )
        await gui_broker._handle_execution_report(report)
        sent_json = json.loads(gui_broker._gui_server.send_to.call_args[0][1])
        # The GUI should see the original order's cl_ord_id
        assert sent_json["cl_ord_id"] == "GUI-1"

    @pytest.mark.asyncio
    async def test_unknown_client_broadcasts(self, gui_broker):
        report = execution_report(
            "UNKNOWN", "OM-999", ExecType.New, OrdStatus.New,
            "BTC/USD", Side.Buy, 1.0, 0.0, 0.0,
        )
        await gui_broker._handle_execution_report(report)
        gui_broker._gui_server.broadcast.assert_called_once()
