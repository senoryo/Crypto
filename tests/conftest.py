"""Shared fixtures for the crypto trading test suite."""

import copy
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.config import DEFAULT_RISK_LIMITS
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
)


# ---------------------------------------------------------------------------
# FIXMessage fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def buy_limit_order():
    """A standard BUY limit order for BTC/USD."""
    return new_order_single(
        cl_ord_id="GUI-1",
        symbol="BTC/USD",
        side=Side.Buy,
        qty=1.0,
        ord_type=OrdType.Limit,
        price=67000.0,
        exchange="BINANCE",
    )


@pytest.fixture
def sell_limit_order():
    """A standard SELL limit order for ETH/USD."""
    return new_order_single(
        cl_ord_id="GUI-2",
        symbol="ETH/USD",
        side=Side.Sell,
        qty=10.0,
        ord_type=OrdType.Limit,
        price=3500.0,
        exchange="BINANCE",
    )


@pytest.fixture
def market_order():
    """A BUY market order for SOL/USD."""
    return new_order_single(
        cl_ord_id="GUI-3",
        symbol="SOL/USD",
        side=Side.Buy,
        qty=100.0,
        ord_type=OrdType.Market,
    )


@pytest.fixture
def new_ack_report():
    """An ExecutionReport acknowledging a new order."""
    return execution_report(
        cl_ord_id="GUI-1",
        order_id="OM-000001",
        exec_type=ExecType.New,
        ord_status=OrdStatus.New,
        symbol="BTC/USD",
        side=Side.Buy,
        leaves_qty=1.0,
        cum_qty=0.0,
        avg_px=0.0,
    )


@pytest.fixture
def fill_execution_report():
    """An ExecutionReport for a trade fill."""
    return execution_report(
        cl_ord_id="GUI-1",
        order_id="OM-000001",
        exec_type=ExecType.Trade,
        ord_status=OrdStatus.Filled,
        symbol="BTC/USD",
        side=Side.Buy,
        leaves_qty=0.0,
        cum_qty=1.0,
        avg_px=67000.0,
        last_px=67000.0,
        last_qty=1.0,
    )


# ---------------------------------------------------------------------------
# Risk limits fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def risk_limits():
    """A deep copy of DEFAULT_RISK_LIMITS for isolated test modification."""
    return copy.deepcopy(DEFAULT_RISK_LIMITS)


# ---------------------------------------------------------------------------
# Mock WebSocket fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_websocket():
    """A mock WebSocket server connection."""
    ws = AsyncMock()
    ws.remote_address = ("127.0.0.1", 9999)
    ws.send = AsyncMock()
    return ws


@pytest.fixture
def mock_ws_client():
    """A mock WSClient instance."""
    client = MagicMock()
    client.send = AsyncMock()
    client.connect = AsyncMock()
    client.listen = AsyncMock()
    client.close = AsyncMock()
    client._ws = MagicMock()
    return client


@pytest.fixture
def mock_ws_server():
    """A mock WSServer instance."""
    server = MagicMock()
    server.send_to = AsyncMock()
    server.broadcast = AsyncMock()
    server.start = AsyncMock()
    server.stop = AsyncMock()
    server.clients = set()
    return server
