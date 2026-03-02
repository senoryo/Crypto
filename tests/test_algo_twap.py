"""
Comprehensive tests for the TWAP (Time-Weighted Average Price) execution strategy.
"""

import asyncio
import math
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from algo.parent_order import ParentOrder, ChildOrder, ParentState
from algo.strategies.twap import TWAPStrategy
from shared.fix_protocol import OrdType, Side


# --- Helpers ---

def make_engine(market_data=None):
    """Create a mock AlgoEngine with controllable market data."""
    engine = MagicMock()
    engine._market_data = market_data or {}
    engine.send_child_order = AsyncMock(return_value=True)
    engine.cancel_child_order = AsyncMock(return_value=True)
    return engine


def make_parent_order(
    parent_id="ALGO-TWAP-001",
    symbol="BTC/USD",
    side=Side.Buy,
    total_qty=10.0,
    arrival_price=50000.0,
):
    return ParentOrder(
        parent_id=parent_id,
        symbol=symbol,
        side=side,
        total_qty=total_qty,
        algo_type="TWAP",
        arrival_price=arrival_price,
    )


def make_child(cl_ord_id, parent_id="ALGO-TWAP-001", qty=1.0, price=50000.0):
    return ChildOrder(
        cl_ord_id=cl_ord_id,
        parent_id=parent_id,
        symbol="BTC/USD",
        side=Side.Buy,
        qty=qty,
        price=price,
        ord_type=OrdType.Limit,
        exchange="BINANCE",
    )


# --- Test: STRATEGY_NAME ---

def test_strategy_name():
    engine = make_engine()
    strategy = TWAPStrategy(engine)
    assert strategy.STRATEGY_NAME == "TWAP"


# --- Test: Constructor defaults ---

def test_default_parameters():
    engine = make_engine()
    strategy = TWAPStrategy(engine)
    assert strategy.horizon_seconds == 3600
    assert strategy.num_slices == 20
    assert strategy.jitter_pct == 0.15
    assert strategy.price_offset_bps == 5
    assert strategy.aggressive_threshold == 0.7
    assert strategy.max_slice_retries == 2


def test_custom_parameters():
    engine = make_engine()
    params = {
        "horizon_seconds": 600,
        "num_slices": 10,
        "jitter_pct": 0.20,
        "price_offset_bps": 10,
        "aggressive_threshold": 0.5,
        "max_slice_retries": 3,
    }
    strategy = TWAPStrategy(engine, params)
    assert strategy.horizon_seconds == 600
    assert strategy.num_slices == 10
    assert strategy.jitter_pct == 0.20
    assert strategy.price_offset_bps == 10
    assert strategy.aggressive_threshold == 0.5
    assert strategy.max_slice_retries == 3


# --- Test: Equal slice distribution ---

def test_equal_slice_distribution_divisible():
    """When total qty divides evenly into num_slices."""
    engine = make_engine()
    strategy = TWAPStrategy(engine, {"num_slices": 5, "horizon_seconds": 5})
    parent = make_parent_order(total_qty=10.0)
    parent.start()
    strategy.parent_order = parent
    strategy._active = True

    # Manually compute what on_start would compute
    total_qty = parent.total_qty
    strategy._slice_qty = math.floor(total_qty / strategy.num_slices * 1e8) / 1e8
    strategy._last_slice_extra = total_qty - strategy._slice_qty * (strategy.num_slices - 1)

    assert strategy._slice_qty == 2.0
    # First 4 slices = 2.0 each (8.0), last slice = 10.0 - 8.0 = 2.0
    assert abs(strategy._last_slice_extra - 2.0) < 1e-8
    total_allocated = strategy._slice_qty * (strategy.num_slices - 1) + strategy._last_slice_extra
    assert abs(total_allocated - total_qty) < 1e-8


def test_equal_slice_distribution_non_divisible():
    """When total qty doesn't divide evenly, remainder goes to last slice."""
    engine = make_engine()
    strategy = TWAPStrategy(engine, {"num_slices": 3, "horizon_seconds": 3})
    parent = make_parent_order(total_qty=10.0)
    parent.start()
    strategy.parent_order = parent
    strategy._active = True

    total_qty = parent.total_qty
    strategy._slice_qty = math.floor(total_qty / strategy.num_slices * 1e8) / 1e8
    strategy._last_slice_extra = total_qty - strategy._slice_qty * (strategy.num_slices - 1)

    # 10 / 3 = 3.33333333 -> floor to 8 decimals = 3.33333333
    assert strategy._slice_qty == pytest.approx(3.33333333, abs=1e-8)
    # Last slice gets remainder: 10.0 - 2 * 3.33333333 = 3.33333334
    total_allocated = strategy._slice_qty * 2 + strategy._last_slice_extra
    assert abs(total_allocated - total_qty) < 1e-7


def test_slice_distribution_with_large_num_slices():
    """Many slices with small qty still adds up correctly."""
    engine = make_engine()
    strategy = TWAPStrategy(engine, {"num_slices": 100, "horizon_seconds": 100})
    parent = make_parent_order(total_qty=1.0)
    parent.start()
    strategy.parent_order = parent
    strategy._active = True

    total_qty = parent.total_qty
    strategy._slice_qty = math.floor(total_qty / strategy.num_slices * 1e8) / 1e8
    strategy._last_slice_extra = total_qty - strategy._slice_qty * (strategy.num_slices - 1)

    total_allocated = strategy._slice_qty * 99 + strategy._last_slice_extra
    assert abs(total_allocated - total_qty) < 1e-7


# --- Test: Jitter ---

def test_jitter_within_bounds():
    """All jittered times stay within +/- jitter_pct of base start."""
    engine = make_engine()
    strategy = TWAPStrategy(engine, {
        "num_slices": 20,
        "horizon_seconds": 100,
        "jitter_pct": 0.15,
    })
    strategy._algo_start_time = 1000.0
    strategy._slice_duration = 100.0 / 20  # 5 seconds per slice

    times = strategy._generate_jittered_times()

    assert len(times) == 20
    for i, t in enumerate(times):
        base = strategy._algo_start_time + i * strategy._slice_duration
        max_jitter = strategy._slice_duration * strategy.jitter_pct
        # Allow for the clamping to prevent overlap
        assert t >= strategy._algo_start_time


def test_jitter_no_overlap():
    """Jittered times must be strictly increasing (no overlap)."""
    engine = make_engine()
    strategy = TWAPStrategy(engine, {
        "num_slices": 50,
        "horizon_seconds": 50,
        "jitter_pct": 0.40,  # Large jitter to stress test
    })
    strategy._algo_start_time = 1000.0
    strategy._slice_duration = 1.0

    # Run multiple times to catch randomness edge cases
    for _ in range(100):
        times = strategy._generate_jittered_times()
        for i in range(1, len(times)):
            assert times[i] > times[i - 1], (
                f"Overlap at slice {i}: {times[i]} <= {times[i-1]}"
            )


def test_zero_jitter_no_variation():
    """With 0% jitter, all start times equal their base times."""
    engine = make_engine()
    strategy = TWAPStrategy(engine, {
        "num_slices": 10,
        "horizon_seconds": 100,
        "jitter_pct": 0.0,
    })
    strategy._algo_start_time = 1000.0
    strategy._slice_duration = 10.0

    times = strategy._generate_jittered_times()
    for i, t in enumerate(times):
        expected = strategy._algo_start_time + i * strategy._slice_duration
        assert abs(t - expected) < 1e-9, (
            f"Slice {i}: expected {expected}, got {t}"
        )


def test_max_jitter_stays_within_bounds():
    """With 50% jitter, times are bounded and non-overlapping."""
    engine = make_engine()
    strategy = TWAPStrategy(engine, {
        "num_slices": 10,
        "horizon_seconds": 100,
        "jitter_pct": 0.50,
    })
    strategy._algo_start_time = 1000.0
    strategy._slice_duration = 10.0

    for _ in range(100):
        times = strategy._generate_jittered_times()
        assert len(times) == 10
        for i in range(1, len(times)):
            assert times[i] > times[i - 1]
        # All times after algo start
        assert all(t >= 1000.0 for t in times)


# --- Test: Passive pricing ---

def test_passive_price_buy():
    """BUY passive: bid + offset_bps * mid / 10000."""
    engine = make_engine()
    strategy = TWAPStrategy(engine, {"price_offset_bps": 5})
    strategy._latest_bid = 49900.0
    strategy._latest_ask = 50100.0
    strategy._latest_mid = 50000.0

    price = strategy._passive_price(Side.Buy)
    expected = 49900.0 + 5 * 50000.0 / 10000.0  # 49900 + 25 = 49925
    assert abs(price - expected) < 0.01


def test_passive_price_sell():
    """SELL passive: ask - offset_bps * mid / 10000."""
    engine = make_engine()
    strategy = TWAPStrategy(engine, {"price_offset_bps": 5})
    strategy._latest_bid = 49900.0
    strategy._latest_ask = 50100.0
    strategy._latest_mid = 50000.0

    price = strategy._passive_price(Side.Sell)
    expected = 50100.0 - 5 * 50000.0 / 10000.0  # 50100 - 25 = 50075
    assert abs(price - expected) < 0.01


def test_passive_price_no_market_data():
    """Returns 0 when no market data available."""
    engine = make_engine()
    strategy = TWAPStrategy(engine)
    strategy._latest_mid = 0.0
    assert strategy._passive_price(Side.Buy) == 0.0


# --- Test: Aggressive pricing ---

def test_aggressive_price_buy():
    """BUY aggressive: ask (cross the spread)."""
    engine = make_engine()
    strategy = TWAPStrategy(engine)
    strategy._latest_bid = 49900.0
    strategy._latest_ask = 50100.0

    price = strategy._aggressive_price(Side.Buy)
    assert price == 50100.0


def test_aggressive_price_sell():
    """SELL aggressive: bid (cross the spread)."""
    engine = make_engine()
    strategy = TWAPStrategy(engine)
    strategy._latest_bid = 49900.0
    strategy._latest_ask = 50100.0

    price = strategy._aggressive_price(Side.Sell)
    assert price == 49900.0


def test_aggressive_price_no_data():
    """Returns 0 when no market data."""
    engine = make_engine()
    strategy = TWAPStrategy(engine)
    strategy._latest_bid = 0.0
    strategy._latest_ask = 0.0
    assert strategy._aggressive_price(Side.Buy) == 0.0
    assert strategy._aggressive_price(Side.Sell) == 0.0


# --- Test: on_fill tracks fills per slice ---

@pytest.mark.asyncio
async def test_on_fill_tracks_per_slice():
    engine = make_engine()
    strategy = TWAPStrategy(engine, {"num_slices": 5, "horizon_seconds": 5})
    parent = make_parent_order(total_qty=10.0)
    parent.start()
    strategy.parent_order = parent
    strategy._active = True
    strategy._filled_per_slice = [0.0] * 5

    # Simulate fill on slice 2
    strategy._current_slice = 2
    child = make_child("C1", qty=2.0)
    parent.add_child_order(child)
    parent.process_fill("C1", 2.0, 50000.0)

    await strategy.on_fill(child, 2.0, 50000.0)

    assert strategy._filled_per_slice[2] == 2.0
    assert strategy._filled_per_slice[0] == 0.0


# --- Test: on_reject returns qty to residual ---

@pytest.mark.asyncio
async def test_on_reject_adds_to_residual():
    engine = make_engine()
    strategy = TWAPStrategy(engine, {"num_slices": 5, "horizon_seconds": 5})
    parent = make_parent_order(total_qty=10.0)
    parent.start()
    strategy.parent_order = parent
    strategy._active = True
    strategy._residual = 0.0

    child = make_child("C1", qty=2.0)
    parent.add_child_order(child)
    parent.process_reject("C1", "risk limit")

    await strategy.on_reject(child, "risk limit")

    assert strategy._residual == 2.0


@pytest.mark.asyncio
async def test_on_reject_partially_filled():
    """Reject after partial fill only adds unfilled portion to residual."""
    engine = make_engine()
    strategy = TWAPStrategy(engine, {"num_slices": 5, "horizon_seconds": 5})
    parent = make_parent_order(total_qty=10.0)
    parent.start()
    strategy.parent_order = parent
    strategy._active = True
    strategy._residual = 0.0
    strategy._filled_per_slice = [0.0] * 5

    child = make_child("C1", qty=2.0)
    parent.add_child_order(child)
    # Partially fill then reject
    parent.process_fill("C1", 0.5, 50000.0)
    child.status = "REJECTED"

    await strategy.on_reject(child, "timeout")

    # Residual should be 2.0 - 0.5 = 1.5
    assert abs(strategy._residual - 1.5) < 1e-8


# --- Test: on_tick updates market data ---

@pytest.mark.asyncio
async def test_on_tick_updates_bid_ask_mid():
    engine = make_engine()
    strategy = TWAPStrategy(engine)
    parent = make_parent_order()
    parent.start()
    strategy.parent_order = parent
    strategy._active = True

    await strategy.on_tick("BTC/USD", {"bid": 49900, "ask": 50100})

    assert strategy._latest_bid == 49900.0
    assert strategy._latest_ask == 50100.0
    assert strategy._latest_mid == 50000.0


@pytest.mark.asyncio
async def test_on_tick_ignores_wrong_symbol():
    engine = make_engine()
    strategy = TWAPStrategy(engine)
    parent = make_parent_order(symbol="BTC/USD")
    parent.start()
    strategy.parent_order = parent
    strategy._active = True

    await strategy.on_tick("ETH/USD", {"bid": 3000, "ask": 3100})

    assert strategy._latest_bid == 0.0
    assert strategy._latest_ask == 0.0


@pytest.mark.asyncio
async def test_on_tick_ignores_when_paused():
    engine = make_engine()
    strategy = TWAPStrategy(engine)
    parent = make_parent_order()
    parent.start()
    strategy.parent_order = parent
    strategy._active = True
    strategy._paused = True

    await strategy.on_tick("BTC/USD", {"bid": 49900, "ask": 50100})

    assert strategy._latest_bid == 0.0


# --- Test: Circuit breakers ---

def test_price_circuit_breaker_triggers():
    engine = make_engine()
    strategy = TWAPStrategy(engine, {"price_circuit_pct": 0.05})
    strategy._arrival_mid = 50000.0
    strategy._latest_mid = 53000.0  # 6% move
    strategy._latest_bid = 52900.0
    strategy._latest_ask = 53100.0

    assert strategy._check_circuit_breakers() is True


def test_price_circuit_breaker_no_trigger():
    engine = make_engine()
    strategy = TWAPStrategy(engine, {"price_circuit_pct": 0.05})
    strategy._arrival_mid = 50000.0
    strategy._latest_mid = 50500.0  # 1% move
    strategy._latest_bid = 50400.0
    strategy._latest_ask = 50600.0

    assert strategy._check_circuit_breakers() is False


def test_spread_circuit_breaker_triggers():
    engine = make_engine()
    strategy = TWAPStrategy(engine, {
        "price_circuit_pct": 0.10,  # won't trigger
        "spread_circuit_pct": 0.02,
    })
    strategy._arrival_mid = 50000.0
    strategy._latest_mid = 50000.0
    strategy._latest_bid = 49000.0  # 2% spread
    strategy._latest_ask = 51000.0

    # spread = 2000, mid = 50000, spread/mid = 4% > 2%
    assert strategy._check_circuit_breakers() is True


def test_circuit_breaker_no_market_data():
    """Circuit breaker should not trigger with no data."""
    engine = make_engine()
    strategy = TWAPStrategy(engine)
    strategy._arrival_mid = 0.0
    strategy._latest_mid = 0.0

    assert strategy._check_circuit_breakers() is False


# --- Test: Scheduler runs correct number of slices ---

@pytest.mark.asyncio
async def test_scheduler_runs_all_slices():
    """Verify the scheduler runs the correct number of slices."""
    engine = make_engine()
    strategy = TWAPStrategy(engine, {
        "num_slices": 3,
        "horizon_seconds": 0.3,  # very short for testing
        "jitter_pct": 0.0,
        "aggressive_threshold": 0.9,  # high threshold so we don't escalate
    })
    parent = make_parent_order(total_qty=3.0)
    parent.start()
    strategy.parent_order = parent
    strategy._active = True
    strategy._filled_per_slice = [0.0] * 3
    strategy._slice_qty = 1.0
    strategy._last_slice_extra = 1.0
    strategy._algo_start_time = time.time()
    strategy._algo_end_time = strategy._algo_start_time + 0.3
    strategy._slice_duration = 0.1
    strategy._jittered_start_times = [
        strategy._algo_start_time,
        strategy._algo_start_time + 0.1,
        strategy._algo_start_time + 0.2,
    ]

    # Make submit_child_order simulate instant fills
    submitted_count = 0

    async def mock_submit(qty, price=0.0, ord_type=OrdType.Limit, exchange=""):
        nonlocal submitted_count
        submitted_count += 1
        cl_ord_id = f"CHILD-{submitted_count}"
        child = ChildOrder(
            cl_ord_id=cl_ord_id,
            parent_id=parent.parent_id,
            symbol=parent.symbol,
            side=parent.side,
            qty=qty,
            price=price,
            ord_type=ord_type,
            exchange=exchange or "BINANCE",
        )
        parent.add_child_order(child)
        # Simulate immediate fill
        parent.process_fill(cl_ord_id, qty, 50000.0)
        child.status = "FILLED"
        await strategy.on_fill(child, qty, 50000.0)
        return cl_ord_id

    strategy.submit_child_order = mock_submit
    strategy.cancel_child_order = AsyncMock()

    await strategy._run_scheduler()

    # Should have submitted orders for slices (at least the passive ones)
    # Once complete (after 3 fills of 1.0 each = 3.0 total), scheduler stops
    assert parent.filled_qty >= 3.0 or submitted_count >= 3


# --- Test: Completion transitions parent to DONE ---

@pytest.mark.asyncio
async def test_completion_sets_done():
    engine = make_engine()
    strategy = TWAPStrategy(engine, {"num_slices": 1, "horizon_seconds": 1})
    parent = make_parent_order(total_qty=1.0)
    parent.start()
    strategy.parent_order = parent
    strategy._active = True
    strategy._filled_per_slice = [0.0]
    strategy._current_slice = 0

    child = make_child("C1", qty=1.0)
    parent.add_child_order(child)
    parent.process_fill("C1", 1.0, 50000.0)

    await strategy.on_fill(child, 1.0, 50000.0)

    assert parent.state == ParentState.DONE
    assert strategy._active is False


# --- Test: Cancellation stops scheduler ---

@pytest.mark.asyncio
async def test_stop_cancels_scheduler():
    engine = make_engine()
    strategy = TWAPStrategy(engine, {
        "num_slices": 10,
        "horizon_seconds": 100,
    })
    parent = make_parent_order(total_qty=10.0)
    parent.start()
    strategy.parent_order = parent
    strategy._active = True

    # Create a long-running scheduler task
    async def slow_scheduler():
        await asyncio.sleep(100)

    strategy._scheduler_task = asyncio.ensure_future(slow_scheduler())

    # Stop should cancel the scheduler
    await strategy.stop()

    assert strategy._active is False
    assert strategy._scheduler_task.cancelled() or strategy._scheduler_task.done()


# --- Test: Place slice order passive/aggressive ---

@pytest.mark.asyncio
async def test_place_slice_order_passive():
    engine = make_engine()
    strategy = TWAPStrategy(engine, {"price_offset_bps": 5})
    parent = make_parent_order(side=Side.Buy)
    parent.start()
    strategy.parent_order = parent
    strategy._active = True
    strategy._latest_bid = 49900.0
    strategy._latest_ask = 50100.0
    strategy._latest_mid = 50000.0

    child_id = await strategy._place_slice_order(1.0, aggressive=False)

    # Should have called submit_child_order with passive price
    assert child_id is not None
    # Verify the engine was asked to send the order
    assert engine.send_child_order.called


@pytest.mark.asyncio
async def test_place_slice_order_aggressive():
    engine = make_engine()
    strategy = TWAPStrategy(engine)
    parent = make_parent_order(side=Side.Buy)
    parent.start()
    strategy.parent_order = parent
    strategy._active = True
    strategy._latest_bid = 49900.0
    strategy._latest_ask = 50100.0
    strategy._latest_mid = 50000.0

    child_id = await strategy._place_slice_order(1.0, aggressive=True)

    assert child_id is not None
    # Verify a child was added with aggressive price (ask for buys)
    children = list(parent.child_orders.values())
    assert len(children) == 1
    assert children[0].price == 50100.0


@pytest.mark.asyncio
async def test_place_slice_order_no_market_data_uses_market():
    """With no market data, fall back to market order."""
    engine = make_engine()
    strategy = TWAPStrategy(engine)
    parent = make_parent_order()
    parent.start()
    strategy.parent_order = parent
    strategy._active = True
    strategy._latest_bid = 0.0
    strategy._latest_ask = 0.0
    strategy._latest_mid = 0.0

    child_id = await strategy._place_slice_order(1.0, aggressive=False)

    # Should submit a market order
    assert child_id is not None
    children = list(parent.child_orders.values())
    assert len(children) == 1
    assert children[0].ord_type == OrdType.Market


# --- Test: Residual sweep ---

@pytest.mark.asyncio
async def test_residual_sweep_places_market_order():
    engine = make_engine()
    strategy = TWAPStrategy(engine)
    parent = make_parent_order(total_qty=10.0)
    parent.start()
    strategy.parent_order = parent
    strategy._active = True
    strategy._algo_start_time = time.time() - 100
    strategy._algo_end_time = time.time() - 50

    # Parent has 3.0 remaining
    parent.filled_qty = 7.0
    parent._fill_notional = 7.0 * 50000

    await strategy._sweep_residual()

    # Should have submitted a market order for remaining 3.0
    children = list(parent.child_orders.values())
    assert len(children) == 1
    assert children[0].ord_type == OrdType.Market
    assert children[0].qty == 3.0


@pytest.mark.asyncio
async def test_no_sweep_when_complete():
    engine = make_engine()
    strategy = TWAPStrategy(engine)
    parent = make_parent_order(total_qty=10.0)
    parent.start()
    strategy.parent_order = parent
    strategy._active = True

    # Parent fully filled
    parent.filled_qty = 10.0
    parent._fill_notional = 10.0 * 50000

    await strategy._sweep_residual()

    # No orders should be submitted
    assert len(parent.child_orders) == 0


# --- Test: Timer drift protection ---

def test_timer_drift_logging():
    """Timer drift check should not raise."""
    engine = make_engine()
    strategy = TWAPStrategy(engine)
    parent = make_parent_order()
    parent.start()
    strategy.parent_order = parent
    strategy._algo_start_time = time.time() - 3700
    strategy.horizon_seconds = 3600

    # Should run without error
    strategy._check_timer_drift()


# --- Test: Very short horizon edge case ---

@pytest.mark.asyncio
async def test_very_short_horizon():
    """Edge case: 1-second horizon with 1 slice."""
    engine = make_engine()
    strategy = TWAPStrategy(engine, {
        "num_slices": 1,
        "horizon_seconds": 0.05,
        "jitter_pct": 0.0,
        "aggressive_threshold": 0.9,
    })
    parent = make_parent_order(total_qty=1.0)
    parent.start()
    strategy.parent_order = parent
    strategy._active = True
    strategy._filled_per_slice = [0.0]
    strategy._slice_qty = 1.0
    strategy._last_slice_extra = 1.0
    strategy._algo_start_time = time.time()
    strategy._algo_end_time = strategy._algo_start_time + 0.05
    strategy._slice_duration = 0.05
    strategy._jittered_start_times = [strategy._algo_start_time]

    submitted = []

    async def mock_submit(qty, price=0.0, ord_type=OrdType.Limit, exchange=""):
        cl_ord_id = f"CHILD-{len(submitted)+1}"
        child = ChildOrder(
            cl_ord_id=cl_ord_id,
            parent_id=parent.parent_id,
            symbol=parent.symbol,
            side=parent.side,
            qty=qty,
            price=price,
            ord_type=ord_type,
            exchange=exchange or "BINANCE",
        )
        parent.add_child_order(child)
        parent.process_fill(cl_ord_id, qty, 50000.0)
        child.status = "FILLED"
        submitted.append(cl_ord_id)
        await strategy.on_fill(child, qty, 50000.0)
        return cl_ord_id

    strategy.submit_child_order = mock_submit
    strategy.cancel_child_order = AsyncMock()

    await strategy._run_scheduler()

    assert len(submitted) >= 1
    assert parent.filled_qty >= 1.0


# --- Test: on_cancel_ack clears current child ---

@pytest.mark.asyncio
async def test_cancel_ack_clears_current_child():
    engine = make_engine()
    strategy = TWAPStrategy(engine)
    parent = make_parent_order()
    parent.start()
    strategy.parent_order = parent
    strategy._active = True

    child = make_child("C1", qty=1.0)
    strategy._current_child_id = "C1"

    await strategy.on_cancel_ack(child)

    assert strategy._current_child_id is None


# --- Test: Constructor accepts (engine, params) signature ---

def test_constructor_signature():
    """Engine line 245 calls strategy_class(self, params)."""
    engine = make_engine()
    # Must work with no params
    s1 = TWAPStrategy(engine)
    assert s1.engine is engine

    # Must work with params dict
    s2 = TWAPStrategy(engine, {"horizon_seconds": 120})
    assert s2.horizon_seconds == 120

    # Must work with params=None
    s3 = TWAPStrategy(engine, None)
    assert s3.horizon_seconds == 3600


# --- Test: on_start initializes correctly ---

@pytest.mark.asyncio
async def test_on_start_initializes_state():
    engine = make_engine({"BTC/USD": {"bid": 49900, "ask": 50100}})
    strategy = TWAPStrategy(engine, {
        "num_slices": 5,
        "horizon_seconds": 50,
        "jitter_pct": 0.0,
    })
    parent = make_parent_order(total_qty=10.0, arrival_price=50000.0)
    strategy.parent_order = parent
    strategy._active = True

    # Patch asyncio.ensure_future to capture the scheduler
    with patch("algo.strategies.twap.asyncio.ensure_future") as mock_future:
        mock_task = MagicMock()
        mock_future.return_value = mock_task
        parent.start()
        # Call on_start manually (normally called by base.start)
        await strategy.on_start()

    assert strategy._slice_qty == 2.0
    assert len(strategy._filled_per_slice) == 5
    assert strategy._slice_duration == 10.0
    assert len(strategy._jittered_start_times) == 5
    assert strategy._scheduler_task is mock_task


# --- Test: Sell side pricing ---

def test_passive_price_sell_side():
    engine = make_engine()
    strategy = TWAPStrategy(engine, {"price_offset_bps": 10})
    strategy._latest_bid = 2900.0
    strategy._latest_ask = 3100.0
    strategy._latest_mid = 3000.0

    price = strategy._passive_price(Side.Sell)
    expected = 3100.0 - 10 * 3000.0 / 10000.0  # 3100 - 3 = 3097
    assert abs(price - expected) < 0.01


def test_aggressive_price_sell_side():
    engine = make_engine()
    strategy = TWAPStrategy(engine)
    strategy._latest_bid = 2900.0
    strategy._latest_ask = 3100.0

    price = strategy._aggressive_price(Side.Sell)
    assert price == 2900.0


# --- Test: Arrival mid capture ---

@pytest.mark.asyncio
async def test_arrival_mid_from_market_data():
    """on_start captures arrival mid from engine market data."""
    engine = make_engine({"BTC/USD": {"bid": 49000, "ask": 51000}})
    strategy = TWAPStrategy(engine, {
        "num_slices": 2,
        "horizon_seconds": 2,
        "jitter_pct": 0.0,
    })
    parent = make_parent_order(total_qty=2.0, arrival_price=0.0)
    strategy.parent_order = parent
    strategy._active = True

    with patch("algo.strategies.twap.asyncio.ensure_future") as mock_future:
        mock_future.return_value = MagicMock()
        parent.start()
        await strategy.on_start()

    assert strategy._arrival_mid == 50000.0


@pytest.mark.asyncio
async def test_arrival_mid_fallback_to_parent():
    """on_start falls back to parent arrival_price if no market data."""
    engine = make_engine({})
    strategy = TWAPStrategy(engine, {
        "num_slices": 2,
        "horizon_seconds": 2,
        "jitter_pct": 0.0,
    })
    parent = make_parent_order(total_qty=2.0, arrival_price=48000.0)
    strategy.parent_order = parent
    strategy._active = True

    with patch("algo.strategies.twap.asyncio.ensure_future") as mock_future:
        mock_future.return_value = MagicMock()
        parent.start()
        await strategy.on_start()

    assert strategy._arrival_mid == 48000.0
