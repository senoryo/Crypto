"""
Comprehensive tests for the AlgoEngine (algo/engine.py).

Tests cover:
- submit_algo creates ParentOrder and strategy
- send_child_order builds correct FIX message
- Execution report routing (fill, reject, cancel, new)
- Market data distribution to strategies
- Rate limiting blocks excess orders
- Global limit enforcement (max concurrent, max notional, max orders per algo)
- kill_all cancels all child orders
- Pause/resume lifecycle
- Control server command routing
- Status reporting
- Dead man's switch heartbeat monitoring
- Edge cases and error handling
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from algo.engine import AlgoEngine, RateLimiter, DEFAULT_MAX_ORDERS_PER_SECOND
from algo.parent_order import ParentOrder, ChildOrder, ParentState
from algo.strategies.base import BaseStrategy
from shared.fix_protocol import (
    FIXMessage, Tag, MsgType, ExecType, OrdStatus, OrdType, Side,
    execution_report,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class DummyStrategy(BaseStrategy):
    """Minimal strategy for testing."""

    STRATEGY_NAME = "DUMMY"

    def __init__(self, engine, params=None):
        super().__init__(engine)
        self.params = params or {}
        self.started = False
        self.ticks: list[tuple[str, dict]] = []
        self.fills: list[tuple[ChildOrder, float, float]] = []
        self.rejects: list[tuple[ChildOrder, str]] = []
        self.cancel_acks: list[ChildOrder] = []

    async def on_start(self) -> None:
        self.started = True

    async def on_tick(self, symbol: str, market_data: dict) -> None:
        self.ticks.append((symbol, market_data))

    async def on_fill(self, child_order: ChildOrder, fill_qty: float, fill_price: float) -> None:
        self.fills.append((child_order, fill_qty, fill_price))

    async def on_reject(self, child_order: ChildOrder, reason: str) -> None:
        self.rejects.append((child_order, reason))

    async def on_cancel_ack(self, child_order: ChildOrder) -> None:
        self.cancel_acks.append(child_order)


def _make_engine(**kwargs) -> AlgoEngine:
    """Create an AlgoEngine with mocked WS connections."""
    engine = AlgoEngine(**kwargs)
    # Mock WebSocket clients so nothing connects to real servers
    engine._mktdata_client = MagicMock()
    engine._mktdata_client.send = AsyncMock()
    engine._mktdata_client.close = AsyncMock()
    engine._om_client = MagicMock()
    engine._om_client.send = AsyncMock()
    engine._om_client.close = AsyncMock()
    engine._server = MagicMock()
    engine._server.start = AsyncMock()
    engine._server.stop = AsyncMock()
    engine._server.send_to = AsyncMock()
    # Mark connections as up
    engine._mktdata_connected = True
    engine._om_connected = True
    engine._running = True
    # Register the dummy strategy
    engine.register_strategy("DUMMY", DummyStrategy)
    return engine


def _make_exec_report(
    cl_ord_id: str,
    exec_type: str,
    last_qty: float = 0.0,
    last_px: float = 0.0,
    cum_qty: float = 0.0,
    leaves_qty: float = 0.0,
    text: str = "",
) -> str:
    """Build a FIX ExecutionReport JSON string."""
    msg = execution_report(
        cl_ord_id=cl_ord_id,
        order_id="ORD-1",
        exec_type=exec_type,
        ord_status=OrdStatus.New,
        symbol="BTC/USD",
        side=Side.Buy,
        leaves_qty=leaves_qty,
        cum_qty=cum_qty,
        avg_px=0.0,
        last_px=last_px,
        last_qty=last_qty,
        text=text,
    )
    return msg.to_json()


# ---------------------------------------------------------------------------
# Tests: submit_algo
# ---------------------------------------------------------------------------

class TestSubmitAlgo:
    @pytest.mark.asyncio
    async def test_submit_creates_parent_order_and_strategy(self):
        engine = _make_engine()
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 5.0)
        assert pid is not None
        assert pid.startswith("ALGO-DUMMY-")
        assert pid in engine._strategies
        assert pid in engine._parent_orders
        strategy = engine._strategies[pid]
        assert isinstance(strategy, DummyStrategy)
        assert strategy.started is True
        assert strategy.parent_order is not None
        assert strategy.parent_order.symbol == "BTC/USD"
        assert strategy.parent_order.side == Side.Buy
        assert strategy.parent_order.total_qty == 5.0
        assert strategy.parent_order.state == ParentState.ACTIVE

    @pytest.mark.asyncio
    async def test_submit_unknown_algo_type_returns_none(self):
        engine = _make_engine()
        pid = await engine.submit_algo("NONEXISTENT", "BTC/USD", Side.Buy, 1.0)
        assert pid is None

    @pytest.mark.asyncio
    async def test_submit_invalid_qty_returns_none(self):
        engine = _make_engine()
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 0)
        assert pid is None
        pid2 = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, -5)
        assert pid2 is None

    @pytest.mark.asyncio
    async def test_submit_om_disconnected_returns_none(self):
        engine = _make_engine()
        engine._om_connected = False
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 1.0)
        assert pid is None

    @pytest.mark.asyncio
    async def test_submit_captures_arrival_price(self):
        engine = _make_engine()
        engine._market_data["BTC/USD"] = {"bid": 50000, "ask": 50100}
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 1.0)
        assert pid is not None
        parent = engine._parent_orders[pid]
        assert parent.arrival_price == pytest.approx(50050.0)

    @pytest.mark.asyncio
    async def test_submit_no_market_data_arrival_price_zero(self):
        engine = _make_engine()
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 1.0)
        assert pid is not None
        parent = engine._parent_orders[pid]
        assert parent.arrival_price == 0.0

    @pytest.mark.asyncio
    async def test_submit_returns_unique_parent_ids(self):
        engine = _make_engine()
        ids = []
        for _ in range(5):
            pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 1.0)
            ids.append(pid)
        assert len(set(ids)) == 5


# ---------------------------------------------------------------------------
# Tests: send_child_order
# ---------------------------------------------------------------------------

class TestSendChildOrder:
    @pytest.mark.asyncio
    async def test_send_child_order_builds_correct_fix(self):
        engine = _make_engine()
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 10.0)
        child = ChildOrder(
            cl_ord_id="ALGO-DUMMY-001-1",
            parent_id=pid,
            symbol="BTC/USD",
            side=Side.Buy,
            qty=2.5,
            price=50000.0,
            ord_type=OrdType.Limit,
            exchange="BINANCE",
        )
        engine._parent_orders[pid].add_child_order(child)
        result = await engine.send_child_order(child)
        assert result is True
        # Verify FIX message was sent
        engine._om_client.send.assert_called_once()
        sent_json = engine._om_client.send.call_args[0][0]
        fix_msg = FIXMessage.from_json(sent_json)
        assert fix_msg.msg_type == MsgType.NewOrderSingle
        assert fix_msg.get(Tag.ClOrdID) == "ALGO-DUMMY-001-1"
        assert fix_msg.get(Tag.Symbol) == "BTC/USD"
        assert fix_msg.get(Tag.Side) == Side.Buy
        assert fix_msg.get(Tag.OrderQty) == "2.5"
        assert fix_msg.get(Tag.OrdType) == OrdType.Limit
        assert fix_msg.get(Tag.Price) == "50000.0"
        assert fix_msg.get(Tag.ExDestination) == "BINANCE"

    @pytest.mark.asyncio
    async def test_send_child_order_registers_mapping(self):
        engine = _make_engine()
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 10.0)
        child = ChildOrder(
            cl_ord_id="ALGO-DUMMY-001-1",
            parent_id=pid,
            symbol="BTC/USD",
            side=Side.Buy,
            qty=1.0,
            price=50000.0,
            ord_type=OrdType.Limit,
            exchange="BINANCE",
        )
        engine._parent_orders[pid].add_child_order(child)
        await engine.send_child_order(child)
        assert engine._child_to_parent["ALGO-DUMMY-001-1"] == pid

    @pytest.mark.asyncio
    async def test_send_child_order_om_disconnected(self):
        engine = _make_engine()
        engine._om_connected = False
        child = ChildOrder(
            cl_ord_id="TEST-1",
            parent_id="ALGO-DUMMY-001",
            symbol="BTC/USD",
            side=Side.Buy,
            qty=1.0,
            price=50000.0,
            ord_type=OrdType.Limit,
            exchange="BINANCE",
        )
        result = await engine.send_child_order(child)
        assert result is False

    @pytest.mark.asyncio
    async def test_send_child_order_send_exception_returns_false(self):
        engine = _make_engine()
        engine._om_client.send = AsyncMock(side_effect=Exception("WS error"))
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 10.0)
        child = ChildOrder(
            cl_ord_id="ALGO-DUMMY-001-1",
            parent_id=pid,
            symbol="BTC/USD",
            side=Side.Buy,
            qty=1.0,
            price=50000.0,
            ord_type=OrdType.Limit,
            exchange="BINANCE",
        )
        engine._parent_orders[pid].add_child_order(child)
        result = await engine.send_child_order(child)
        assert result is False


# ---------------------------------------------------------------------------
# Tests: cancel_child_order
# ---------------------------------------------------------------------------

class TestCancelChildOrder:
    @pytest.mark.asyncio
    async def test_cancel_sends_fix_cancel_request(self):
        engine = _make_engine()
        child = ChildOrder(
            cl_ord_id="ALGO-DUMMY-001-1",
            parent_id="ALGO-DUMMY-001",
            symbol="BTC/USD",
            side=Side.Buy,
            qty=1.0,
            price=50000.0,
            ord_type=OrdType.Limit,
            exchange="BINANCE",
        )
        result = await engine.cancel_child_order(child)
        assert result is True
        engine._om_client.send.assert_called_once()
        sent_json = engine._om_client.send.call_args[0][0]
        fix_msg = FIXMessage.from_json(sent_json)
        assert fix_msg.msg_type == MsgType.OrderCancelRequest
        assert fix_msg.get(Tag.OrigClOrdID) == "ALGO-DUMMY-001-1"

    @pytest.mark.asyncio
    async def test_cancel_om_disconnected(self):
        engine = _make_engine()
        engine._om_connected = False
        child = ChildOrder(
            cl_ord_id="X-1",
            parent_id="X",
            symbol="BTC/USD",
            side=Side.Buy,
            qty=1.0,
            price=50000.0,
            ord_type=OrdType.Limit,
            exchange="BINANCE",
        )
        result = await engine.cancel_child_order(child)
        assert result is False


# ---------------------------------------------------------------------------
# Tests: execution report routing
# ---------------------------------------------------------------------------

class TestExecReportRouting:
    @pytest.mark.asyncio
    async def test_fill_routed_to_strategy(self):
        engine = _make_engine()
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 10.0)
        strategy = engine._strategies[pid]
        # Submit a child order through the strategy
        cl_ord_id = await strategy.submit_child_order(
            qty=2.0, price=50000.0, ord_type=OrdType.Limit, exchange="BINANCE"
        )
        assert cl_ord_id is not None
        # Simulate a fill execution report
        fill_msg = _make_exec_report(
            cl_ord_id=cl_ord_id,
            exec_type=ExecType.Trade,
            last_qty=2.0,
            last_px=50000.0,
            cum_qty=2.0,
        )
        await engine._on_om_message(fill_msg)
        assert len(strategy.fills) == 1
        assert strategy.fills[0][1] == 2.0
        assert strategy.fills[0][2] == 50000.0

    @pytest.mark.asyncio
    async def test_reject_routed_to_strategy(self):
        engine = _make_engine()
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 10.0)
        strategy = engine._strategies[pid]
        cl_ord_id = await strategy.submit_child_order(
            qty=1.0, price=50000.0, ord_type=OrdType.Limit, exchange="BINANCE"
        )
        reject_msg = _make_exec_report(
            cl_ord_id=cl_ord_id,
            exec_type=ExecType.Rejected,
            text="Risk limit",
        )
        await engine._on_om_message(reject_msg)
        assert len(strategy.rejects) == 1
        assert strategy.rejects[0][1] == "Risk limit"

    @pytest.mark.asyncio
    async def test_cancel_ack_routed_to_strategy(self):
        engine = _make_engine()
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 10.0)
        strategy = engine._strategies[pid]
        cl_ord_id = await strategy.submit_child_order(
            qty=1.0, price=50000.0, ord_type=OrdType.Limit, exchange="BINANCE"
        )
        cancel_msg = _make_exec_report(
            cl_ord_id=cl_ord_id,
            exec_type=ExecType.Canceled,
        )
        await engine._on_om_message(cancel_msg)
        assert len(strategy.cancel_acks) == 1

    @pytest.mark.asyncio
    async def test_new_ack_updates_child_status(self):
        engine = _make_engine()
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 10.0)
        strategy = engine._strategies[pid]
        cl_ord_id = await strategy.submit_child_order(
            qty=1.0, price=50000.0, ord_type=OrdType.Limit, exchange="BINANCE"
        )
        new_msg = _make_exec_report(
            cl_ord_id=cl_ord_id,
            exec_type=ExecType.New,
        )
        await engine._on_om_message(new_msg)
        child = strategy.parent_order.child_orders[cl_ord_id]
        assert child.status == "NEW"

    @pytest.mark.asyncio
    async def test_exec_report_unknown_cl_ord_id_ignored(self):
        engine = _make_engine()
        msg = _make_exec_report(cl_ord_id="UNKNOWN-999", exec_type=ExecType.Trade, last_qty=1.0, last_px=100.0)
        # Should not raise
        await engine._on_om_message(msg)

    @pytest.mark.asyncio
    async def test_non_exec_report_ignored(self):
        engine = _make_engine()
        # A heartbeat message should be silently ignored
        heartbeat = FIXMessage(MsgType.Heartbeat)
        await engine._on_om_message(heartbeat.to_json())


# ---------------------------------------------------------------------------
# Tests: market data distribution
# ---------------------------------------------------------------------------

class TestMarketDataDistribution:
    @pytest.mark.asyncio
    async def test_tick_distributed_to_matching_strategy(self):
        engine = _make_engine()
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 10.0)
        strategy = engine._strategies[pid]
        tick = json.dumps({"symbol": "BTC/USD", "bid": 50000, "ask": 50100})
        await engine._on_mktdata_message(tick)
        assert len(strategy.ticks) == 1
        assert strategy.ticks[0][0] == "BTC/USD"

    @pytest.mark.asyncio
    async def test_tick_not_distributed_to_different_symbol(self):
        engine = _make_engine()
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 10.0)
        strategy = engine._strategies[pid]
        tick = json.dumps({"symbol": "ETH/USD", "bid": 3000, "ask": 3010})
        await engine._on_mktdata_message(tick)
        assert len(strategy.ticks) == 0

    @pytest.mark.asyncio
    async def test_tick_updates_market_data_cache(self):
        engine = _make_engine()
        tick = json.dumps({"symbol": "BTC/USD", "bid": 50000, "ask": 50100})
        await engine._on_mktdata_message(tick)
        assert "BTC/USD" in engine._market_data
        assert engine._market_data["BTC/USD"]["bid"] == 50000

    @pytest.mark.asyncio
    async def test_paused_strategy_does_not_receive_ticks(self):
        engine = _make_engine()
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 10.0)
        strategy = engine._strategies[pid]
        await engine.pause_algo(pid)
        tick = json.dumps({"symbol": "BTC/USD", "bid": 50000, "ask": 50100})
        await engine._on_mktdata_message(tick)
        assert len(strategy.ticks) == 0

    @pytest.mark.asyncio
    async def test_invalid_json_mktdata_ignored(self):
        engine = _make_engine()
        await engine._on_mktdata_message("NOT_JSON{{{")

    @pytest.mark.asyncio
    async def test_mktdata_updates_last_time(self):
        engine = _make_engine()
        old_time = engine._last_mktdata_time
        tick = json.dumps({"symbol": "BTC/USD", "bid": 50000, "ask": 50100})
        await engine._on_mktdata_message(tick)
        assert engine._last_mktdata_time >= old_time


# ---------------------------------------------------------------------------
# Tests: rate limiting
# ---------------------------------------------------------------------------

class TestRateLimiting:
    def test_rate_limiter_allows_under_limit(self):
        rl = RateLimiter(max_per_second=5)
        for _ in range(5):
            assert rl.allow() is True

    def test_rate_limiter_blocks_over_limit(self):
        rl = RateLimiter(max_per_second=3)
        for _ in range(3):
            assert rl.allow() is True
        assert rl.allow() is False

    def test_rate_limiter_reset_clears(self):
        rl = RateLimiter(max_per_second=2)
        rl.allow()
        rl.allow()
        assert rl.allow() is False
        rl.reset()
        assert rl.allow() is True

    def test_rate_limiter_window_sliding(self):
        """Old timestamps older than 1s should not count toward the limit."""
        rl = RateLimiter(max_per_second=2)
        # Manually insert old timestamps beyond the 1-second window
        old_time = time.time() - 2.0
        rl._timestamps = [old_time, old_time]
        # Despite 2 entries, they are stale so new requests should be allowed
        assert rl.allow() is True
        assert rl.allow() is True
        # Now we hit the real limit
        assert rl.allow() is False

    @pytest.mark.asyncio
    async def test_engine_rate_limit_blocks_child_order(self):
        engine = _make_engine(max_orders_per_second=2)
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 100.0)
        parent = engine._parent_orders[pid]
        # Send 2 child orders (allowed)
        for i in range(2):
            child = ChildOrder(
                cl_ord_id=f"C-{i}",
                parent_id=pid,
                symbol="BTC/USD",
                side=Side.Buy,
                qty=1.0,
                price=50000.0,
                ord_type=OrdType.Limit,
                exchange="BINANCE",
            )
            parent.add_child_order(child)
            result = await engine.send_child_order(child)
            assert result is True
        # Third should be blocked
        child3 = ChildOrder(
            cl_ord_id="C-blocked",
            parent_id=pid,
            symbol="BTC/USD",
            side=Side.Buy,
            qty=1.0,
            price=50000.0,
            ord_type=OrdType.Limit,
            exchange="BINANCE",
        )
        parent.add_child_order(child3)
        result = await engine.send_child_order(child3)
        assert result is False


# ---------------------------------------------------------------------------
# Tests: global limits
# ---------------------------------------------------------------------------

class TestGlobalLimits:
    @pytest.mark.asyncio
    async def test_max_concurrent_algos_enforced(self):
        engine = _make_engine(max_concurrent_algos=2)
        pid1 = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 1.0)
        pid2 = await engine.submit_algo("DUMMY", "ETH/USD", Side.Sell, 1.0)
        assert pid1 is not None
        assert pid2 is not None
        # Third should be rejected
        pid3 = await engine.submit_algo("DUMMY", "SOL/USD", Side.Buy, 1.0)
        assert pid3 is None

    @pytest.mark.asyncio
    async def test_max_aggregate_notional_enforced(self):
        engine = _make_engine(max_aggregate_notional=100_000)
        engine._market_data["BTC/USD"] = {"mid": 50000}
        # First algo: 1 BTC = 50k notional (under 100k)
        pid1 = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 1.0)
        assert pid1 is not None
        # Second algo: 2 BTC = 100k notional (total would be 150k, over limit)
        pid2 = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 2.0)
        assert pid2 is None

    @pytest.mark.asyncio
    async def test_max_aggregate_notional_no_price_data_allowed(self):
        engine = _make_engine(max_aggregate_notional=100)
        # No market data means price is 0, can't calculate notional -> passes
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 1000.0)
        assert pid is not None

    @pytest.mark.asyncio
    async def test_max_orders_per_algo_enforced(self):
        engine = _make_engine(max_total_orders_per_algo=2)
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 100.0)
        parent = engine._parent_orders[pid]
        # Add 3 existing child orders to parent (exceeds limit of 2)
        for i in range(3):
            child = ChildOrder(
                cl_ord_id=f"C-existing-{i}",
                parent_id=pid,
                symbol="BTC/USD",
                side=Side.Buy,
                qty=1.0,
                price=50000.0,
                ord_type=OrdType.Limit,
                exchange="BINANCE",
            )
            parent.add_child_order(child)

        # Now try to send another child — should be blocked by max_total_orders
        new_child = ChildOrder(
            cl_ord_id="C-new",
            parent_id=pid,
            symbol="BTC/USD",
            side=Side.Buy,
            qty=1.0,
            price=50000.0,
            ord_type=OrdType.Limit,
            exchange="BINANCE",
        )
        parent.add_child_order(new_child)
        result = await engine.send_child_order(new_child)
        assert result is False


# ---------------------------------------------------------------------------
# Tests: kill_all
# ---------------------------------------------------------------------------

class TestKillAll:
    @pytest.mark.asyncio
    async def test_kill_all_cancels_all_algos(self):
        engine = _make_engine()
        pid1 = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 5.0)
        pid2 = await engine.submit_algo("DUMMY", "ETH/USD", Side.Sell, 10.0)
        strategy1 = engine._strategies[pid1]
        strategy2 = engine._strategies[pid2]
        assert strategy1._active is True
        assert strategy2._active is True

        await engine.kill_all()

        # Strategies should be stopped
        assert strategy1._active is False
        assert strategy2._active is False
        # Parent orders should be cancelled
        assert strategy1.parent_order.state == ParentState.CANCELLED
        assert strategy2.parent_order.state == ParentState.CANCELLED

    @pytest.mark.asyncio
    async def test_kill_all_handles_errors_in_strategy_stop(self):
        engine = _make_engine()
        pid1 = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 5.0)
        pid2 = await engine.submit_algo("DUMMY", "ETH/USD", Side.Sell, 10.0)
        # Make first strategy's stop() raise an error
        strategy1 = engine._strategies[pid1]
        original_stop = strategy1.stop

        async def broken_stop():
            raise RuntimeError("stop failed")

        strategy1.stop = broken_stop
        # kill_all should not propagate the error, second strategy should still stop
        await engine.kill_all()
        strategy2 = engine._strategies[pid2]
        assert strategy2._active is False

    @pytest.mark.asyncio
    async def test_kill_all_no_algos_noop(self):
        engine = _make_engine()
        # Should not raise
        await engine.kill_all()


# ---------------------------------------------------------------------------
# Tests: pause / resume lifecycle
# ---------------------------------------------------------------------------

class TestPauseResume:
    @pytest.mark.asyncio
    async def test_pause_algo(self):
        engine = _make_engine()
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 5.0)
        strategy = engine._strategies[pid]
        assert strategy._paused is False
        assert strategy.parent_order.state == ParentState.ACTIVE

        result = await engine.pause_algo(pid)
        assert result is True
        assert strategy._paused is True
        assert strategy.parent_order.state == ParentState.PAUSED

    @pytest.mark.asyncio
    async def test_resume_algo(self):
        engine = _make_engine()
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 5.0)
        strategy = engine._strategies[pid]
        await engine.pause_algo(pid)
        assert strategy._paused is True

        result = await engine.resume_algo(pid)
        assert result is True
        assert strategy._paused is False
        assert strategy.parent_order.state == ParentState.ACTIVE

    @pytest.mark.asyncio
    async def test_pause_unknown_algo_returns_false(self):
        engine = _make_engine()
        result = await engine.pause_algo("NONEXISTENT")
        assert result is False

    @pytest.mark.asyncio
    async def test_resume_unknown_algo_returns_false(self):
        engine = _make_engine()
        result = await engine.resume_algo("NONEXISTENT")
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_algo(self):
        engine = _make_engine()
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 5.0)
        strategy = engine._strategies[pid]
        result = await engine.cancel_algo(pid)
        assert result is True
        assert strategy._active is False
        assert strategy.parent_order.state == ParentState.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_unknown_algo_returns_false(self):
        engine = _make_engine()
        result = await engine.cancel_algo("NONEXISTENT")
        assert result is False


# ---------------------------------------------------------------------------
# Tests: control server
# ---------------------------------------------------------------------------

class TestControlServer:
    @pytest.mark.asyncio
    async def test_control_submit(self):
        engine = _make_engine()
        ws = MagicMock()
        cmd = json.dumps({"action": "submit", "algo_type": "DUMMY", "symbol": "BTC/USD", "side": "1", "qty": 5})
        await engine._on_control_message(ws, cmd)
        engine._server.send_to.assert_called_once()
        response = json.loads(engine._server.send_to.call_args[0][1])
        assert response["status"] == "ok"
        assert "parent_order_id" in response

    @pytest.mark.asyncio
    async def test_control_pause(self):
        engine = _make_engine()
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 5.0)
        ws = MagicMock()
        cmd = json.dumps({"action": "pause", "parent_order_id": pid})
        await engine._on_control_message(ws, cmd)
        response = json.loads(engine._server.send_to.call_args[0][1])
        assert response["status"] == "ok"

    @pytest.mark.asyncio
    async def test_control_resume(self):
        engine = _make_engine()
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 5.0)
        await engine.pause_algo(pid)
        ws = MagicMock()
        cmd = json.dumps({"action": "resume", "parent_order_id": pid})
        await engine._on_control_message(ws, cmd)
        response = json.loads(engine._server.send_to.call_args[0][1])
        assert response["status"] == "ok"

    @pytest.mark.asyncio
    async def test_control_cancel(self):
        engine = _make_engine()
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 5.0)
        ws = MagicMock()
        cmd = json.dumps({"action": "cancel", "parent_order_id": pid})
        await engine._on_control_message(ws, cmd)
        response = json.loads(engine._server.send_to.call_args[0][1])
        assert response["status"] == "ok"

    @pytest.mark.asyncio
    async def test_control_kill_all(self):
        engine = _make_engine()
        await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 5.0)
        ws = MagicMock()
        cmd = json.dumps({"action": "kill_all"})
        await engine._on_control_message(ws, cmd)
        response = json.loads(engine._server.send_to.call_args[0][1])
        assert response["status"] == "ok"

    @pytest.mark.asyncio
    async def test_control_status(self):
        engine = _make_engine()
        await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 5.0)
        ws = MagicMock()
        cmd = json.dumps({"action": "status"})
        await engine._on_control_message(ws, cmd)
        response = json.loads(engine._server.send_to.call_args[0][1])
        assert response["status"] == "ok"
        assert isinstance(response["data"], list)
        assert len(response["data"]) == 1

    @pytest.mark.asyncio
    async def test_control_algo_status(self):
        engine = _make_engine()
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 5.0)
        ws = MagicMock()
        cmd = json.dumps({"action": "algo_status", "parent_order_id": pid})
        await engine._on_control_message(ws, cmd)
        response = json.loads(engine._server.send_to.call_args[0][1])
        assert response["status"] == "ok"
        assert response["data"]["parent_id"] == pid

    @pytest.mark.asyncio
    async def test_control_algo_status_not_found(self):
        engine = _make_engine()
        ws = MagicMock()
        cmd = json.dumps({"action": "algo_status", "parent_order_id": "NONEXISTENT"})
        await engine._on_control_message(ws, cmd)
        response = json.loads(engine._server.send_to.call_args[0][1])
        assert response["status"] == "error"

    @pytest.mark.asyncio
    async def test_control_unknown_action(self):
        engine = _make_engine()
        ws = MagicMock()
        cmd = json.dumps({"action": "unknown_thing"})
        await engine._on_control_message(ws, cmd)
        response = json.loads(engine._server.send_to.call_args[0][1])
        assert response["status"] == "error"

    @pytest.mark.asyncio
    async def test_control_invalid_json(self):
        engine = _make_engine()
        ws = MagicMock()
        await engine._on_control_message(ws, "NOT_JSON{")
        engine._server.send_to.assert_called_once()
        response = json.loads(engine._server.send_to.call_args[0][1])
        assert "error" in response


# ---------------------------------------------------------------------------
# Tests: status
# ---------------------------------------------------------------------------

class TestStatus:
    @pytest.mark.asyncio
    async def test_get_status_returns_all(self):
        engine = _make_engine()
        await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 5.0)
        await engine.submit_algo("DUMMY", "ETH/USD", Side.Sell, 10.0)
        status = engine.get_status()
        assert len(status) == 2

    @pytest.mark.asyncio
    async def test_get_algo_status_returns_dict(self):
        engine = _make_engine()
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 5.0)
        status = engine.get_algo_status(pid)
        assert status is not None
        assert status["parent_id"] == pid
        assert status["symbol"] == "BTC/USD"
        assert status["state"] == ParentState.ACTIVE

    @pytest.mark.asyncio
    async def test_get_algo_status_unknown_returns_none(self):
        engine = _make_engine()
        status = engine.get_algo_status("NONEXISTENT")
        assert status is None


# ---------------------------------------------------------------------------
# Tests: strategy registration
# ---------------------------------------------------------------------------

class TestStrategyRegistry:
    def test_register_strategy(self):
        engine = _make_engine()
        engine.register_strategy("TEST_ALGO", DummyStrategy)
        assert "TEST_ALGO" in engine._strategy_registry

    @pytest.mark.asyncio
    async def test_registered_strategy_can_be_used(self):
        engine = _make_engine()
        engine.register_strategy("CUSTOM", DummyStrategy)
        pid = await engine.submit_algo("CUSTOM", "BTC/USD", Side.Buy, 1.0)
        assert pid is not None
        assert pid.startswith("ALGO-CUSTOM-")

    def test_builtin_sor_registered(self):
        engine = _make_engine()
        # SOR should be auto-registered by _register_builtins
        assert "SOR" in engine._strategy_registry


# ---------------------------------------------------------------------------
# Tests: dead man's switch
# ---------------------------------------------------------------------------

class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_pause_all_for_disconnect(self):
        engine = _make_engine()
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 5.0)
        strategy = engine._strategies[pid]
        assert strategy._paused is False

        await engine._pause_all_for_disconnect()
        assert strategy._paused is True

    @pytest.mark.asyncio
    async def test_resume_after_reconnect(self):
        engine = _make_engine()
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 5.0)
        strategy = engine._strategies[pid]

        # Simulate disconnect pause
        await engine._pause_all_for_disconnect()
        assert strategy._paused is True
        assert strategy.parent_order.state == ParentState.PAUSED

        # Both connections up -> resume
        engine._mktdata_connected = True
        engine._om_connected = True
        await engine._resume_all_paused_by_disconnect()
        assert strategy._paused is False
        assert strategy.parent_order.state == ParentState.ACTIVE

    @pytest.mark.asyncio
    async def test_no_resume_if_om_still_disconnected(self):
        engine = _make_engine()
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 5.0)
        strategy = engine._strategies[pid]

        await engine._pause_all_for_disconnect()
        engine._mktdata_connected = True
        engine._om_connected = False
        await engine._resume_all_paused_by_disconnect()
        assert strategy._paused is True


# ---------------------------------------------------------------------------
# Tests: edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_engine_default_params(self):
        engine = _make_engine()
        assert engine.max_concurrent_algos == 10
        assert engine.max_aggregate_notional == 10_000_000
        assert engine.max_orders_per_second == 50
        assert engine.max_total_orders_per_algo == 10_000

    @pytest.mark.asyncio
    async def test_engine_custom_params(self):
        engine = _make_engine(
            max_concurrent_algos=5,
            max_aggregate_notional=500_000,
            max_orders_per_second=20,
            max_total_orders_per_algo=100,
        )
        assert engine.max_concurrent_algos == 5
        assert engine.max_aggregate_notional == 500_000
        assert engine.max_orders_per_second == 20
        assert engine.max_total_orders_per_algo == 100

    @pytest.mark.asyncio
    async def test_multiple_strategies_receive_different_ticks(self):
        engine = _make_engine()
        pid_btc = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 1.0)
        pid_eth = await engine.submit_algo("DUMMY", "ETH/USD", Side.Buy, 1.0)
        strat_btc = engine._strategies[pid_btc]
        strat_eth = engine._strategies[pid_eth]

        await engine._on_mktdata_message(json.dumps({"symbol": "BTC/USD", "bid": 50000, "ask": 50100}))
        await engine._on_mktdata_message(json.dumps({"symbol": "ETH/USD", "bid": 3000, "ask": 3010}))

        assert len(strat_btc.ticks) == 1
        assert strat_btc.ticks[0][0] == "BTC/USD"
        assert len(strat_eth.ticks) == 1
        assert strat_eth.ticks[0][0] == "ETH/USD"

    @pytest.mark.asyncio
    async def test_fill_updates_parent_order_aggregates(self):
        engine = _make_engine()
        pid = await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 10.0)
        strategy = engine._strategies[pid]
        cl_ord_id = await strategy.submit_child_order(
            qty=5.0, price=50000.0, ord_type=OrdType.Limit, exchange="BINANCE"
        )
        fill_msg = _make_exec_report(
            cl_ord_id=cl_ord_id,
            exec_type=ExecType.Trade,
            last_qty=3.0,
            last_px=49900.0,
            cum_qty=3.0,
        )
        await engine._on_om_message(fill_msg)
        parent = engine._parent_orders[pid]
        assert parent.filled_qty == 3.0
        assert parent.avg_fill_price == pytest.approx(49900.0)
        assert parent.fill_pct() == pytest.approx(0.3)

    @pytest.mark.asyncio
    async def test_stop_engine(self):
        engine = _make_engine()
        await engine.submit_algo("DUMMY", "BTC/USD", Side.Buy, 5.0)
        await engine.stop()
        assert engine._running is False
        engine._mktdata_client.close.assert_called_once()
        engine._om_client.close.assert_called_once()
        engine._server.stop.assert_called_once()
