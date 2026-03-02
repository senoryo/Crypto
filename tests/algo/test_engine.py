"""Tests for algo/engine.py — AlgoEngine core, routing, safety controls."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from algo.engine import AlgoEngine, RateLimiter, DEFAULT_MAX_ORDERS_PER_SECOND
from algo.parent_order import ParentOrder, ParentState, ChildOrder
from algo.strategies.base import BaseStrategy
from algo.strategies.sor import SmartOrderRouter
from shared.fix_protocol import (
    FIXMessage, Tag, MsgType, ExecType, OrdStatus, OrdType, Side,
    execution_report,
)


# --- RateLimiter tests ---

class TestRateLimiter:

    def test_allows_under_limit(self):
        rl = RateLimiter(max_per_second=5)
        for _ in range(5):
            assert rl.allow()

    def test_blocks_over_limit(self):
        rl = RateLimiter(max_per_second=3)
        for _ in range(3):
            assert rl.allow()
        assert not rl.allow()

    def test_recovers_after_window(self):
        rl = RateLimiter(max_per_second=2)
        rl.allow()
        rl.allow()
        assert not rl.allow()
        # Manually age out old timestamps
        rl._timestamps = [time.time() - 2.0]
        assert rl.allow()

    def test_reset(self):
        rl = RateLimiter(max_per_second=1)
        rl.allow()
        assert not rl.allow()
        rl.reset()
        assert rl.allow()


# --- AlgoEngine tests ---

@pytest.fixture
def engine():
    """Create an AlgoEngine with mocked WS connections."""
    e = AlgoEngine()
    # Mock the WebSocket clients and server
    e._mktdata_client = MagicMock()
    e._mktdata_client.send = AsyncMock()
    e._mktdata_client.close = AsyncMock()
    e._mktdata_client.is_connected = True

    e._om_client = MagicMock()
    e._om_client.send = AsyncMock()
    e._om_client.close = AsyncMock()
    e._om_client.is_connected = True

    e._server = MagicMock()
    e._server.start = AsyncMock()
    e._server.stop = AsyncMock()
    e._server.send_to = AsyncMock()
    e._server.broadcast = AsyncMock()

    e._mktdata_connected = True
    e._om_connected = True
    e._running = True

    # Register SOR strategy (engine no longer auto-registers it)
    e.register_strategy("SOR", SmartOrderRouter)

    # Pre-populate market data for BTC/USD
    e._market_data["BTC/USD"] = {
        "type": "market_data",
        "symbol": "BTC/USD",
        "bid": 50000.0,
        "ask": 50100.0,
        "price": 50050.0,
    }

    return e


class TestAlgoEngineSubmitAlgo:

    @pytest.mark.asyncio
    async def test_start_sor_algo(self, engine):
        parent_id = await engine.submit_algo(
            algo_type="SOR",
            symbol="BTC/USD",
            side=Side.Buy,
            qty=1.0,
        )
        assert parent_id is not None
        assert parent_id.startswith("ALGO-SOR-")
        assert parent_id in engine._strategies

    @pytest.mark.asyncio
    async def test_start_unknown_algo_type(self, engine):
        parent_id = await engine.submit_algo(
            algo_type="UNKNOWN",
            symbol="BTC/USD",
            side=Side.Buy,
            qty=1.0,
        )
        assert parent_id is None

    @pytest.mark.asyncio
    async def test_start_algo_invalid_qty(self, engine):
        parent_id = await engine.submit_algo(
            algo_type="SOR",
            symbol="BTC/USD",
            side=Side.Buy,
            qty=0,
        )
        assert parent_id is None

    @pytest.mark.asyncio
    async def test_start_algo_om_disconnected(self, engine):
        engine._om_connected = False
        parent_id = await engine.submit_algo(
            algo_type="SOR",
            symbol="BTC/USD",
            side=Side.Buy,
            qty=1.0,
        )
        assert parent_id is None

    @pytest.mark.asyncio
    async def test_max_concurrent_strategies(self, engine):
        # Fill up to max (default 10)
        for i in range(10):
            pid = await engine.submit_algo("SOR", "BTC/USD", Side.Buy, 1.0)
            assert pid is not None
        # 11th should fail
        pid = await engine.submit_algo("SOR", "BTC/USD", Side.Buy, 1.0)
        assert pid is None

    @pytest.mark.asyncio
    async def test_arrival_price_captured(self, engine):
        parent_id = await engine.submit_algo(
            algo_type="SOR",
            symbol="BTC/USD",
            side=Side.Buy,
            qty=1.0,
        )
        strategy = engine._strategies[parent_id]
        # Mid of bid=50000, ask=50100 = 50050
        assert strategy.parent_order.arrival_price == 50050.0


class TestAlgoEngineCancelPauseResume:

    @pytest.mark.asyncio
    async def test_cancel_algo(self, engine):
        pid = await engine.submit_algo("SOR", "BTC/USD", Side.Buy, 1.0)
        ok = await engine.cancel_algo(pid)
        assert ok
        assert engine._strategies[pid].parent_order.state == ParentState.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_unknown_algo(self, engine):
        ok = await engine.cancel_algo("NONEXISTENT")
        assert not ok

    @pytest.mark.asyncio
    async def test_pause_resume_algo(self, engine):
        pid = await engine.submit_algo("SOR", "BTC/USD", Side.Buy, 1.0)
        await engine.pause_algo(pid)
        assert engine._strategies[pid].parent_order.state == ParentState.PAUSED
        await engine.resume_algo(pid)
        assert engine._strategies[pid].parent_order.state == ParentState.ACTIVE


class TestAlgoEngineGlobalKill:

    @pytest.mark.asyncio
    async def test_kill_all(self, engine):
        pid1 = await engine.submit_algo("SOR", "BTC/USD", Side.Buy, 1.0)
        pid2 = await engine.submit_algo("SOR", "BTC/USD", Side.Sell, 5.0)
        await engine.kill_all()
        for pid in [pid1, pid2]:
            assert engine._strategies[pid].parent_order.state == ParentState.CANCELLED


class TestAlgoEngineExecReportRouting:

    @pytest.mark.asyncio
    async def test_fill_routed_to_strategy(self, engine):
        pid = await engine.submit_algo("SOR", "BTC/USD", Side.Buy, 1.0)
        strategy = engine._strategies[pid]

        # Find a child order that was submitted
        assert len(strategy.parent_order.child_orders) > 0
        child_id = list(strategy.parent_order.child_orders.keys())[0]
        child = strategy.parent_order.child_orders[child_id]

        # Simulate an execution report from OM
        er = execution_report(
            cl_ord_id=child_id,
            order_id="OM-000001",
            exec_type=ExecType.Trade,
            ord_status=OrdStatus.Filled,
            symbol="BTC/USD",
            side=Side.Buy,
            leaves_qty=0,
            cum_qty=child.qty,
            avg_px=50050.0,
            last_px=50050.0,
            last_qty=child.qty,
        )
        await engine._on_om_message(er.to_json())

        # Verify parent order was updated
        assert strategy.parent_order.filled_qty > 0

    @pytest.mark.asyncio
    async def test_reject_routed_to_strategy(self, engine):
        pid = await engine.submit_algo("SOR", "BTC/USD", Side.Buy, 1.0)
        strategy = engine._strategies[pid]
        child_id = list(strategy.parent_order.child_orders.keys())[0]

        er = execution_report(
            cl_ord_id=child_id,
            order_id="OM-000001",
            exec_type=ExecType.Rejected,
            ord_status=OrdStatus.Rejected,
            symbol="BTC/USD",
            side=Side.Buy,
            leaves_qty=0,
            cum_qty=0,
            avg_px=0,
            text="Risk limit exceeded",
        )
        await engine._on_om_message(er.to_json())

        child = strategy.parent_order.child_orders[child_id]
        assert child.status == "REJECTED"


class TestAlgoEngineRateLimit:

    @pytest.mark.asyncio
    async def test_rate_limit_blocks_excess_orders(self, engine):
        engine._rate_limiter = RateLimiter(max_per_second=2)
        pid = await engine.submit_algo("SOR", "BTC/USD", Side.Buy, 1.0)
        strategy = engine._strategies[pid]

        # Manually submit more children than the rate limit
        child1 = ChildOrder(
            cl_ord_id="TEST-1", parent_id=pid, symbol="BTC/USD",
            side=Side.Buy, qty=0.1, price=50000.0,
            ord_type=OrdType.Limit, exchange="BINANCE",
        )
        child2 = ChildOrder(
            cl_ord_id="TEST-2", parent_id=pid, symbol="BTC/USD",
            side=Side.Buy, qty=0.1, price=50000.0,
            ord_type=OrdType.Limit, exchange="BINANCE",
        )
        child3 = ChildOrder(
            cl_ord_id="TEST-3", parent_id=pid, symbol="BTC/USD",
            side=Side.Buy, qty=0.1, price=50000.0,
            ord_type=OrdType.Limit, exchange="BINANCE",
        )

        # Reset limiter for clean test
        engine._rate_limiter.reset()

        r1 = await engine.send_child_order(child1)
        r2 = await engine.send_child_order(child2)
        r3 = await engine.send_child_order(child3)  # Should be blocked

        assert r1 is True
        assert r2 is True
        assert r3 is False


class TestAlgoEngineStatus:

    @pytest.mark.asyncio
    async def test_get_algo_status(self, engine):
        pid = await engine.submit_algo("SOR", "BTC/USD", Side.Buy, 1.0)
        status = engine.get_algo_status(pid)
        assert status is not None
        assert status["parent_id"] == pid
        assert status["symbol"] == "BTC/USD"

    @pytest.mark.asyncio
    async def test_get_algo_status_unknown(self, engine):
        assert engine.get_algo_status("NONEXISTENT") is None

    @pytest.mark.asyncio
    async def test_get_status(self, engine):
        await engine.submit_algo("SOR", "BTC/USD", Side.Buy, 1.0)
        await engine.submit_algo("SOR", "BTC/USD", Side.Sell, 5.0)
        all_status = engine.get_status()
        assert len(all_status) == 2


class TestAlgoEngineMarketData:

    @pytest.mark.asyncio
    async def test_market_data_updates_cache(self, engine):
        msg = json.dumps({
            "type": "market_data",
            "symbol": "ETH/USD",
            "bid": 3000.0,
            "ask": 3010.0,
            "price": 3005.0,
        })
        await engine._on_mktdata_message(msg)
        assert "ETH/USD" in engine._market_data
        assert engine._market_data["ETH/USD"]["bid"] == 3000.0


class TestAlgoEngineControlServer:

    @pytest.mark.asyncio
    async def test_submit_algo_command(self, engine):
        ws = MagicMock()
        cmd = json.dumps({
            "action": "submit",
            "algo_type": "SOR",
            "symbol": "BTC/USD",
            "side": Side.Buy,
            "qty": 1.0,
        })
        await engine._on_control_message(ws, cmd)
        engine._server.send_to.assert_called_once()
        response = json.loads(engine._server.send_to.call_args[0][1])
        assert response["status"] == "ok"
        assert "parent_order_id" in response

    @pytest.mark.asyncio
    async def test_kill_all_command(self, engine):
        await engine.submit_algo("SOR", "BTC/USD", Side.Buy, 1.0)
        ws = MagicMock()
        cmd = json.dumps({"action": "kill_all"})
        await engine._on_control_message(ws, cmd)
        engine._server.send_to.assert_called_once()
        response = json.loads(engine._server.send_to.call_args[0][1])
        assert response["status"] == "ok"

    @pytest.mark.asyncio
    async def test_invalid_json_command(self, engine):
        ws = MagicMock()
        await engine._on_control_message(ws, "not json")
        engine._server.send_to.assert_called_once()
        response = json.loads(engine._server.send_to.call_args[0][1])
        assert "error" in response

    @pytest.mark.asyncio
    async def test_unknown_command(self, engine):
        ws = MagicMock()
        cmd = json.dumps({"action": "bogus_command"})
        await engine._on_control_message(ws, cmd)
        response = json.loads(engine._server.send_to.call_args[0][1])
        assert response["status"] == "error"
