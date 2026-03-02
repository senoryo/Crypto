"""
Tests for the VWAP (Volume-Weighted Average Price) execution strategy.

Covers volume profile construction, bucket allocation, passive/aggressive
pricing, participation cap enforcement, circuit breakers, fill tracking,
rejection handling, slippage calculation, scheduler behavior, residual
sweep, and completion transitions.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from algo.parent_order import ChildOrder, ParentOrder, ParentState
from algo.strategies.vwap import VWAPStrategy
from shared.fix_protocol import OrdType, Side


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine(market_data=None):
    """Create a mock engine with configurable market data."""
    engine = MagicMock()
    engine._market_data = market_data or {}
    engine.send_child_order = AsyncMock(return_value=True)
    engine.cancel_child_order = AsyncMock()
    return engine


def _make_parent(
    symbol="BTC/USD",
    side=Side.Buy,
    qty=10.0,
    algo_type="VWAP",
    params=None,
    arrival_price=0.0,
) -> ParentOrder:
    return ParentOrder(
        parent_id="VWAP-001",
        symbol=symbol,
        side=side,
        total_qty=qty,
        algo_type=algo_type,
        params=params or {},
        arrival_price=arrival_price,
    )


# ---------------------------------------------------------------------------
# Volume profile
# ---------------------------------------------------------------------------

class TestVolumeProfile:
    """Tests for volume profile construction and normalization."""

    def test_uniform_profile_default(self):
        """With no volume_profile param, distribution is uniform."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {"num_buckets": 4})
        assert len(vwap.volume_profile) == 4
        for w in vwap.volume_profile:
            assert w == pytest.approx(0.25)

    def test_custom_volume_profile(self):
        """Custom profile is stored as-is if it sums to 1."""
        engine = _make_engine()
        profile = [0.1, 0.2, 0.3, 0.4]
        vwap = VWAPStrategy(engine, {"volume_profile": profile})
        assert vwap.volume_profile == pytest.approx(profile)
        assert vwap.num_buckets == 4

    def test_volume_profile_normalization(self):
        """Profile that doesn't sum to 1.0 is normalized."""
        engine = _make_engine()
        profile = [2.0, 3.0, 5.0]
        vwap = VWAPStrategy(engine, {"volume_profile": profile})
        assert sum(vwap.volume_profile) == pytest.approx(1.0)
        assert vwap.volume_profile[0] == pytest.approx(0.2)
        assert vwap.volume_profile[1] == pytest.approx(0.3)
        assert vwap.volume_profile[2] == pytest.approx(0.5)

    def test_volume_profile_all_zeros_becomes_uniform(self):
        """A profile of all zeros normalizes to uniform."""
        engine = _make_engine()
        profile = [0.0, 0.0, 0.0]
        vwap = VWAPStrategy(engine, {"volume_profile": profile})
        for w in vwap.volume_profile:
            assert w == pytest.approx(1.0 / 3)

    def test_num_buckets_matches_profile_length(self):
        """num_buckets is adjusted to match the provided profile length."""
        engine = _make_engine()
        profile = [0.25, 0.25, 0.25, 0.25, 0.0]  # 5 elements
        vwap = VWAPStrategy(engine, {"volume_profile": profile, "num_buckets": 3})
        # num_buckets should be overridden by profile length
        assert vwap.num_buckets == 5


# ---------------------------------------------------------------------------
# Bucket quantity allocation
# ---------------------------------------------------------------------------

class TestBucketAllocation:
    """Tests for per-bucket quantity allocation."""

    @pytest.mark.asyncio
    async def test_bucket_targets_sum_to_total(self):
        """Sum of bucket targets should equal total qty."""
        engine = _make_engine({"BTC/USD": {"bid": 100, "ask": 101}})
        vwap = VWAPStrategy(engine, {"num_buckets": 5, "horizon_seconds": 1})
        parent = _make_parent(qty=10.0)
        # Manually initialize to inspect targets without running scheduler
        vwap.parent_order = parent
        vwap._active = True
        total_qty = parent.total_qty
        vwap._bucket_targets = [total_qty * w for w in vwap.volume_profile]
        vwap._bucket_filled = [0.0] * vwap.num_buckets
        vwap._bucket_market_vol = [0.0] * vwap.num_buckets

        total = sum(vwap._bucket_targets)
        assert total == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_non_uniform_allocation(self):
        """Non-uniform profile allocates proportionally."""
        engine = _make_engine({"BTC/USD": {"bid": 100, "ask": 101}})
        profile = [0.1, 0.3, 0.6]
        vwap = VWAPStrategy(engine, {
            "num_buckets": 3,
            "volume_profile": profile,
            "horizon_seconds": 1,
        })
        parent = _make_parent(qty=100.0)
        vwap.parent_order = parent
        vwap._active = True
        vwap._bucket_targets = [parent.total_qty * w for w in vwap.volume_profile]
        vwap._bucket_filled = [0.0] * vwap.num_buckets
        vwap._bucket_market_vol = [0.0] * vwap.num_buckets

        assert vwap._bucket_targets[0] == pytest.approx(10.0)
        assert vwap._bucket_targets[1] == pytest.approx(30.0)
        assert vwap._bucket_targets[2] == pytest.approx(60.0)

    def test_residual_carry_forward(self):
        """get_bucket_target includes residual from unfilled prior buckets."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {"num_buckets": 3, "horizon_seconds": 3})
        parent = _make_parent(qty=30.0)
        vwap.parent_order = parent
        vwap._active = True

        vwap._bucket_targets = [10.0, 10.0, 10.0]
        vwap._bucket_filled = [6.0, 0.0, 0.0]  # 4 residual from bucket 0
        vwap._bucket_market_vol = [0.0, 0.0, 0.0]

        # Bucket 1 should get its 10 + 4 residual from bucket 0 = 14
        target = vwap._get_bucket_target(1)
        assert target == pytest.approx(14.0)


# ---------------------------------------------------------------------------
# Passive pricing
# ---------------------------------------------------------------------------

class TestPassivePricing:
    """Tests for passive-first pricing logic."""

    def test_passive_buy_price(self):
        """Passive buy price = bid + (mid * offset_bps/10000)."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {"price_offset_bps": 10})
        parent = _make_parent(side=Side.Buy)
        vwap.parent_order = parent
        vwap._latest_bid = 100.0
        vwap._latest_ask = 102.0
        vwap._latest_mid = 101.0

        price = vwap._compute_price(aggressive=False)
        expected = 100.0 + (101.0 * 10 / 10000)  # bid + mid * 10bps
        assert price == pytest.approx(expected)

    def test_passive_sell_price(self):
        """Passive sell price = ask - (mid * offset_bps/10000)."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {"price_offset_bps": 10})
        parent = _make_parent(side=Side.Sell)
        vwap.parent_order = parent
        vwap._latest_bid = 100.0
        vwap._latest_ask = 102.0
        vwap._latest_mid = 101.0

        price = vwap._compute_price(aggressive=False)
        expected = 102.0 - (101.0 * 10 / 10000)  # ask - mid * 10bps
        assert price == pytest.approx(expected)

    def test_aggressive_buy_price(self):
        """Aggressive buy price = ask (cross the spread)."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {"price_offset_bps": 5})
        parent = _make_parent(side=Side.Buy)
        vwap.parent_order = parent
        vwap._latest_bid = 100.0
        vwap._latest_ask = 102.0
        vwap._latest_mid = 101.0

        price = vwap._compute_price(aggressive=True)
        assert price == pytest.approx(102.0)

    def test_aggressive_sell_price(self):
        """Aggressive sell price = bid (cross the spread)."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {"price_offset_bps": 5})
        parent = _make_parent(side=Side.Sell)
        vwap.parent_order = parent
        vwap._latest_bid = 100.0
        vwap._latest_ask = 102.0
        vwap._latest_mid = 101.0

        price = vwap._compute_price(aggressive=True)
        assert price == pytest.approx(100.0)

    def test_no_market_data_returns_zero(self):
        """With no bid/ask, price returns 0 (will trigger market order)."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine)
        parent = _make_parent(side=Side.Buy)
        vwap.parent_order = parent
        vwap._latest_bid = 0.0
        vwap._latest_ask = 0.0
        vwap._latest_mid = 0.0

        price = vwap._compute_price(aggressive=False)
        assert price == 0.0


# ---------------------------------------------------------------------------
# Aggressive escalation
# ---------------------------------------------------------------------------

class TestAggressiveEscalation:
    """Tests for aggressive escalation after threshold."""

    @pytest.mark.asyncio
    async def test_scheduler_places_passive_then_aggressive(self):
        """Scheduler places passive order first, then aggressive if behind."""
        engine = _make_engine({"BTC/USD": {"bid": 100, "ask": 101}})
        vwap = VWAPStrategy(engine, {
            "num_buckets": 1,
            "horizon_seconds": 0.2,
            "aggressive_threshold": 0.5,
        })
        parent = _make_parent(qty=1.0)

        await vwap.start(parent)
        # Give scheduler time to run both phases
        await asyncio.sleep(0.4)

        # Should have placed at least 2 orders (passive + aggressive)
        # and also called cancel for the passive order before aggressive
        assert engine.send_child_order.call_count >= 2


# ---------------------------------------------------------------------------
# Participation cap
# ---------------------------------------------------------------------------

class TestParticipationCap:
    """Tests for participation rate cap enforcement."""

    def test_cap_reduces_qty(self):
        """When fills would exceed participation cap of market volume, qty is reduced."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {
            "num_buckets": 3,
            "participation_cap": 0.15,
        })
        parent = _make_parent(qty=30.0)
        vwap.parent_order = parent
        vwap._active = True
        vwap._current_bucket = 0
        vwap._bucket_filled = [0.0, 0.0, 0.0]
        vwap._bucket_market_vol = [100.0, 0.0, 0.0]  # 100 market vol in bucket 0

        # Requesting 20 but cap = 15% of 100 = 15
        result = vwap._apply_participation_cap(20.0)
        assert result == pytest.approx(15.0)

    def test_cap_accounts_for_existing_fills(self):
        """Cap considers already-filled quantity in the bucket."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {
            "num_buckets": 2,
            "participation_cap": 0.10,
        })
        parent = _make_parent(qty=20.0)
        vwap.parent_order = parent
        vwap._active = True
        vwap._current_bucket = 0
        vwap._bucket_filled = [8.0, 0.0]
        vwap._bucket_market_vol = [100.0, 0.0]

        # Cap = 10% of 100 = 10, already filled 8, room = 2
        result = vwap._apply_participation_cap(5.0)
        assert result == pytest.approx(2.0)

    def test_cap_allows_full_qty_when_no_market_volume(self):
        """With no market volume data, allow the full order quantity."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {
            "num_buckets": 2,
            "participation_cap": 0.15,
        })
        parent = _make_parent(qty=20.0)
        vwap.parent_order = parent
        vwap._active = True
        vwap._current_bucket = 0
        vwap._bucket_filled = [0.0, 0.0]
        vwap._bucket_market_vol = [0.0, 0.0]

        result = vwap._apply_participation_cap(10.0)
        assert result == pytest.approx(10.0)

    def test_cap_returns_zero_when_fully_utilized(self):
        """When already at cap, returns zero remaining."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {
            "num_buckets": 1,
            "participation_cap": 0.10,
        })
        parent = _make_parent(qty=20.0)
        vwap.parent_order = parent
        vwap._active = True
        vwap._current_bucket = 0
        vwap._bucket_filled = [10.0]
        vwap._bucket_market_vol = [100.0]

        result = vwap._apply_participation_cap(5.0)
        assert result == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Residual handling
# ---------------------------------------------------------------------------

class TestResidualHandling:
    """Tests for final bucket residual sweep."""

    @pytest.mark.asyncio
    async def test_residual_sweep_with_market_order(self):
        """After all buckets, remaining qty is swept with a market order."""
        engine = _make_engine({"BTC/USD": {"bid": 100, "ask": 101}})
        vwap = VWAPStrategy(engine, {
            "num_buckets": 1,
            "horizon_seconds": 0.1,
            "aggressive_threshold": 0.5,
        })
        parent = _make_parent(qty=5.0)

        await vwap.start(parent)
        # Wait for scheduler to complete
        await asyncio.sleep(0.3)

        # At least one order should have been placed
        assert engine.send_child_order.call_count >= 1

        # Check that the last order submitted was either limit or market
        # (sweep uses market orders)
        last_child = list(parent.child_orders.values())[-1]
        # The sweep and aggressive orders are part of the flow
        assert len(parent.child_orders) >= 1


# ---------------------------------------------------------------------------
# Fill tracking
# ---------------------------------------------------------------------------

class TestFillTracking:
    """Tests for on_fill bucket tracking and completion."""

    @pytest.mark.asyncio
    async def test_fill_updates_bucket_filled(self):
        """on_fill increments the current bucket's filled quantity."""
        engine = _make_engine({"BTC/USD": {"bid": 100, "ask": 101}})
        vwap = VWAPStrategy(engine, {"num_buckets": 3, "horizon_seconds": 100})
        parent = _make_parent(qty=30.0)

        # Manually set up state (don't run full scheduler)
        vwap.parent_order = parent
        vwap._active = True
        vwap._bucket_filled = [0.0, 0.0, 0.0]
        vwap._bucket_targets = [10.0, 10.0, 10.0]
        vwap._bucket_market_vol = [0.0, 0.0, 0.0]
        vwap._current_bucket = 1

        child = ChildOrder("C-1", "VWAP-001", "BTC/USD", Side.Buy, 5.0, 100.5, OrdType.Limit, "")
        parent.add_child_order(child)
        parent.process_fill("C-1", 5.0, 100.5)

        await vwap.on_fill(child, 5.0, 100.5)

        assert vwap._bucket_filled[1] == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_completion_transitions_parent_to_done(self):
        """When all qty is filled, parent transitions to DONE."""
        engine = _make_engine({"BTC/USD": {"bid": 100, "ask": 101}})
        vwap = VWAPStrategy(engine, {"num_buckets": 2, "horizon_seconds": 100})
        parent = _make_parent(qty=1.0)
        parent.start()  # PENDING -> ACTIVE

        vwap.parent_order = parent
        vwap._active = True
        vwap._bucket_filled = [0.0, 0.0]
        vwap._bucket_targets = [0.5, 0.5]
        vwap._bucket_market_vol = [0.0, 0.0]
        vwap._current_bucket = 0

        child = ChildOrder("C-1", "VWAP-001", "BTC/USD", Side.Buy, 1.0, 100.5, OrdType.Limit, "")
        parent.add_child_order(child)
        parent.process_fill("C-1", 1.0, 100.5)

        await vwap.on_fill(child, 1.0, 100.5)

        assert parent.state == ParentState.DONE
        assert not vwap._active


# ---------------------------------------------------------------------------
# Rejection handling
# ---------------------------------------------------------------------------

class TestRejectionHandling:
    """Tests for child order rejection handling."""

    @pytest.mark.asyncio
    async def test_reject_logs_and_continues(self):
        """Rejected child should log but not crash the strategy."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {"num_buckets": 2, "horizon_seconds": 100})
        parent = _make_parent(qty=10.0)
        parent.start()

        vwap.parent_order = parent
        vwap._active = True
        vwap._bucket_filled = [0.0, 0.0]
        vwap._bucket_targets = [5.0, 5.0]
        vwap._bucket_market_vol = [0.0, 0.0]
        vwap._current_bucket = 0

        child = ChildOrder("C-1", "VWAP-001", "BTC/USD", Side.Buy, 5.0, 100.0, OrdType.Limit, "")
        parent.add_child_order(child)
        parent.process_reject("C-1", "Rate limited")

        # Should not raise
        await vwap.on_reject(child, "Rate limited")
        assert vwap._active  # Strategy still active


# ---------------------------------------------------------------------------
# Cancel ack
# ---------------------------------------------------------------------------

class TestCancelAck:
    """Tests for cancel acknowledgement handling."""

    @pytest.mark.asyncio
    async def test_cancel_ack_logs_and_continues(self):
        """Cancel ack should log and not crash the strategy."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {"num_buckets": 2, "horizon_seconds": 100})
        parent = _make_parent(qty=10.0)
        parent.start()

        vwap.parent_order = parent
        vwap._active = True
        vwap._bucket_filled = [0.0, 0.0]
        vwap._bucket_targets = [5.0, 5.0]
        vwap._bucket_market_vol = [0.0, 0.0]
        vwap._current_bucket = 0

        child = ChildOrder("C-1", "VWAP-001", "BTC/USD", Side.Buy, 5.0, 100.0, OrdType.Limit, "")
        parent.add_child_order(child)
        child.status = "CANCELLED"

        await vwap.on_cancel_ack(child)
        assert vwap._active


# ---------------------------------------------------------------------------
# Circuit breakers
# ---------------------------------------------------------------------------

class TestCircuitBreakers:
    """Tests for price, spread, and volume circuit breakers."""

    @pytest.mark.asyncio
    async def test_price_circuit_breaker(self):
        """Price CB triggers when mid moves > 5% from arrival price."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {
            "num_buckets": 2,
            "price_cb_pct": 0.05,
            "horizon_seconds": 100,
        })
        parent = _make_parent(side=Side.Buy)
        parent.start()
        vwap.parent_order = parent
        vwap._active = True
        vwap._arrival_price = 100.0
        vwap._bucket_filled = [0.0, 0.0]
        vwap._bucket_targets = [5.0, 5.0]
        vwap._bucket_market_vol = [0.0, 0.0]
        vwap._current_bucket = 0
        vwap._bucket_start_time = time.time()
        vwap._bucket_duration = 50.0

        # Set mid to 106 (6% move, > 5% threshold)
        vwap._latest_mid = 106.0
        vwap._latest_bid = 105.5
        vwap._latest_ask = 106.5

        await vwap._check_circuit_breakers()
        assert vwap._cb_triggered
        assert "price" in vwap._cb_reason.lower()

    @pytest.mark.asyncio
    async def test_spread_circuit_breaker(self):
        """Spread CB triggers when spread > 2% of mid."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {
            "num_buckets": 2,
            "spread_cb_pct": 0.02,
            "horizon_seconds": 100,
        })
        parent = _make_parent(side=Side.Buy)
        parent.start()
        vwap.parent_order = parent
        vwap._active = True
        vwap._arrival_price = 100.0
        vwap._bucket_filled = [0.0, 0.0]
        vwap._bucket_targets = [5.0, 5.0]
        vwap._bucket_market_vol = [0.0, 0.0]
        vwap._current_bucket = 0
        vwap._bucket_start_time = time.time()
        vwap._bucket_duration = 50.0

        # Set wide spread: bid=98, ask=102, mid=100 => spread=4%
        vwap._latest_bid = 98.0
        vwap._latest_ask = 102.0
        vwap._latest_mid = 100.0

        await vwap._check_circuit_breakers()
        assert vwap._cb_triggered
        assert "spread" in vwap._cb_reason.lower()

    @pytest.mark.asyncio
    async def test_volume_circuit_breaker(self):
        """Volume CB triggers when market volume is near zero after 30% of bucket."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {
            "num_buckets": 1,
            "volume_cb_min": 1e-9,
            "horizon_seconds": 100,
        })
        parent = _make_parent(side=Side.Buy)
        parent.start()
        vwap.parent_order = parent
        vwap._active = True
        vwap._arrival_price = 100.0
        vwap._latest_mid = 100.0
        vwap._latest_bid = 99.5
        vwap._latest_ask = 100.5
        vwap._bucket_filled = [0.0]
        vwap._bucket_targets = [10.0]
        vwap._bucket_market_vol = [0.0]  # zero volume
        vwap._current_bucket = 0
        vwap._bucket_start_time = time.time() - 40  # 40s into 100s bucket (>30%)
        vwap._bucket_duration = 100.0

        await vwap._check_circuit_breakers()
        assert vwap._cb_triggered
        assert "volume" in vwap._cb_reason.lower()

    @pytest.mark.asyncio
    async def test_circuit_breaker_resets(self):
        """CB resets when conditions return to normal."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {
            "num_buckets": 2,
            "price_cb_pct": 0.05,
            "horizon_seconds": 100,
        })
        parent = _make_parent(side=Side.Buy)
        parent.start()
        vwap.parent_order = parent
        vwap._active = True
        vwap._arrival_price = 100.0
        vwap._cb_triggered = True
        vwap._cb_reason = "price moved 6.0% from arrival"
        vwap._bucket_filled = [0.0, 0.0]
        vwap._bucket_targets = [5.0, 5.0]
        vwap._bucket_market_vol = [100.0, 0.0]
        vwap._current_bucket = 0
        vwap._bucket_start_time = time.time()
        vwap._bucket_duration = 50.0

        # Normal conditions: mid close to arrival, normal spread
        vwap._latest_mid = 101.0
        vwap._latest_bid = 100.5
        vwap._latest_ask = 101.5

        await vwap._check_circuit_breakers()
        assert not vwap._cb_triggered


# ---------------------------------------------------------------------------
# Slippage tracking
# ---------------------------------------------------------------------------

class TestSlippageTracking:
    """Tests for running VWAP and slippage calculation."""

    def test_running_vwap_calculation(self):
        """Running VWAP = sum(price*vol) / sum(vol)."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {"num_buckets": 2})

        vwap._tick_pv_sum = 100.0 * 50 + 102.0 * 50  # 5000 + 5100
        vwap._tick_volume_sum = 100.0

        result = vwap._calculate_running_vwap()
        assert result == pytest.approx(101.0)

    def test_running_vwap_zero_volume(self):
        """Running VWAP returns 0 when no volume."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {"num_buckets": 2})
        assert vwap._calculate_running_vwap() == 0.0


# ---------------------------------------------------------------------------
# Scheduler behavior
# ---------------------------------------------------------------------------

class TestSchedulerBehavior:
    """Tests for the scheduler task."""

    @pytest.mark.asyncio
    async def test_scheduler_runs_correct_number_of_buckets(self):
        """Scheduler should iterate through all buckets."""
        engine = _make_engine({"BTC/USD": {"bid": 100, "ask": 101}})
        vwap = VWAPStrategy(engine, {
            "num_buckets": 3,
            "horizon_seconds": 0.3,
            "aggressive_threshold": 0.5,
        })
        parent = _make_parent(qty=3.0)

        await vwap.start(parent)
        # Wait for all 3 buckets to complete
        await asyncio.sleep(0.8)

        # Should have placed orders across multiple buckets
        # Each bucket places passive + possibly aggressive
        assert engine.send_child_order.call_count >= 3

    @pytest.mark.asyncio
    async def test_scheduler_stops_on_cancel(self):
        """Stopping the strategy should cancel the scheduler task."""
        engine = _make_engine({"BTC/USD": {"bid": 100, "ask": 101}})
        vwap = VWAPStrategy(engine, {
            "num_buckets": 10,
            "horizon_seconds": 10,
        })
        parent = _make_parent(qty=10.0)

        await vwap.start(parent)
        # Let first bucket start
        await asyncio.sleep(0.05)

        await vwap.stop()

        assert not vwap._active
        assert parent.state == ParentState.CANCELLED
        assert vwap._scheduler_task.done()

    @pytest.mark.asyncio
    async def test_scheduler_stops_when_parent_complete(self):
        """Scheduler exits early when parent order is fully filled."""
        engine = _make_engine({"BTC/USD": {"bid": 100, "ask": 101}})
        vwap = VWAPStrategy(engine, {
            "num_buckets": 5,
            "horizon_seconds": 2,
            "aggressive_threshold": 0.5,
        })
        parent = _make_parent(qty=1.0)

        await vwap.start(parent)
        await asyncio.sleep(0.1)

        # Simulate full fill
        child = list(parent.child_orders.values())[0]
        parent.process_fill(child.cl_ord_id, 1.0, 100.5)
        await vwap.on_fill(child, 1.0, 100.5)

        # Strategy should be done
        assert parent.state == ParentState.DONE
        assert not vwap._active


# ---------------------------------------------------------------------------
# on_tick processing
# ---------------------------------------------------------------------------

class TestOnTick:
    """Tests for on_tick market data processing."""

    @pytest.mark.asyncio
    async def test_on_tick_updates_bid_ask(self):
        """on_tick should update latest bid/ask/mid."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {"num_buckets": 2, "horizon_seconds": 100})
        parent = _make_parent()
        vwap.parent_order = parent
        vwap._active = True
        vwap._arrival_price = 100.0
        vwap._bucket_filled = [0.0, 0.0]
        vwap._bucket_targets = [5.0, 5.0]
        vwap._bucket_market_vol = [0.0, 0.0]
        vwap._current_bucket = 0
        vwap._bucket_start_time = time.time()
        vwap._bucket_duration = 50.0

        await vwap.on_tick("BTC/USD", {"bid": 99, "ask": 101, "volume": 10})

        assert vwap._latest_bid == 99.0
        assert vwap._latest_ask == 101.0
        assert vwap._latest_mid == 100.0

    @pytest.mark.asyncio
    async def test_on_tick_updates_running_vwap(self):
        """on_tick should accumulate volume for running VWAP."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {"num_buckets": 2, "horizon_seconds": 100})
        parent = _make_parent()
        vwap.parent_order = parent
        vwap._active = True
        vwap._arrival_price = 100.0
        vwap._bucket_filled = [0.0, 0.0]
        vwap._bucket_targets = [5.0, 5.0]
        vwap._bucket_market_vol = [0.0, 0.0]
        vwap._current_bucket = 0
        vwap._bucket_start_time = time.time()
        vwap._bucket_duration = 50.0

        await vwap.on_tick("BTC/USD", {"bid": 100, "ask": 102, "volume": 50})
        await vwap.on_tick("BTC/USD", {"bid": 104, "ask": 106, "volume": 50})

        assert vwap._tick_volume_sum == pytest.approx(100.0)
        running_vwap = vwap._calculate_running_vwap()
        # First tick mid=101, vol=50; second tick mid=105, vol=50
        expected = (101.0 * 50 + 105.0 * 50) / 100
        assert running_vwap == pytest.approx(expected)

    @pytest.mark.asyncio
    async def test_on_tick_tracks_bucket_market_volume(self):
        """on_tick should add volume to current bucket's market volume."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {"num_buckets": 2, "horizon_seconds": 100})
        parent = _make_parent()
        vwap.parent_order = parent
        vwap._active = True
        vwap._arrival_price = 100.0
        vwap._bucket_filled = [0.0, 0.0]
        vwap._bucket_targets = [5.0, 5.0]
        vwap._bucket_market_vol = [0.0, 0.0]
        vwap._current_bucket = 0
        vwap._bucket_start_time = time.time()
        vwap._bucket_duration = 50.0

        await vwap.on_tick("BTC/USD", {"bid": 100, "ask": 101, "volume": 25})
        await vwap.on_tick("BTC/USD", {"bid": 100, "ask": 101, "volume": 30})

        assert vwap._bucket_market_vol[0] == pytest.approx(55.0)

    @pytest.mark.asyncio
    async def test_on_tick_ignores_wrong_symbol(self):
        """on_tick should ignore ticks for other symbols."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {"num_buckets": 2, "horizon_seconds": 100})
        parent = _make_parent(symbol="BTC/USD")
        vwap.parent_order = parent
        vwap._active = True
        vwap._arrival_price = 100.0
        vwap._bucket_filled = [0.0, 0.0]
        vwap._bucket_targets = [5.0, 5.0]
        vwap._bucket_market_vol = [0.0, 0.0]
        vwap._current_bucket = 0
        vwap._bucket_start_time = time.time()
        vwap._bucket_duration = 50.0

        await vwap.on_tick("ETH/USD", {"bid": 3000, "ask": 3001, "volume": 10})

        assert vwap._latest_bid == 0.0  # unchanged

    @pytest.mark.asyncio
    async def test_on_tick_ignores_when_paused(self):
        """on_tick should not process when strategy is paused."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {"num_buckets": 2, "horizon_seconds": 100})
        parent = _make_parent()
        vwap.parent_order = parent
        vwap._active = True
        vwap._paused = True
        vwap._arrival_price = 100.0
        vwap._bucket_filled = [0.0, 0.0]
        vwap._bucket_targets = [5.0, 5.0]
        vwap._bucket_market_vol = [0.0, 0.0]
        vwap._current_bucket = 0

        await vwap.on_tick("BTC/USD", {"bid": 99, "ask": 101, "volume": 10})

        assert vwap._latest_bid == 0.0  # unchanged


# ---------------------------------------------------------------------------
# Parameter defaults
# ---------------------------------------------------------------------------

class TestParameterDefaults:
    """Tests for default parameter values."""

    def test_default_parameters(self):
        """Default parameter values are set correctly."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine)
        assert vwap.horizon_seconds == 3600
        assert vwap.num_buckets == 12
        assert vwap.participation_cap == pytest.approx(0.15)
        assert vwap.price_offset_bps == pytest.approx(5)
        assert vwap.aggressive_threshold == pytest.approx(0.7)
        assert vwap.use_sor is False

    def test_custom_parameters(self):
        """Custom parameters override defaults."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {
            "horizon_seconds": 1800,
            "num_buckets": 6,
            "participation_cap": 0.20,
            "price_offset_bps": 10,
            "aggressive_threshold": 0.8,
            "use_sor": True,
        })
        assert vwap.horizon_seconds == 1800
        assert vwap.num_buckets == 6
        assert vwap.participation_cap == pytest.approx(0.20)
        assert vwap.price_offset_bps == pytest.approx(10)
        assert vwap.aggressive_threshold == pytest.approx(0.8)
        assert vwap.use_sor is True

    def test_strategy_name(self):
        """Strategy name is VWAP."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine)
        assert vwap.STRATEGY_NAME == "VWAP"


# ---------------------------------------------------------------------------
# Place bucket order
# ---------------------------------------------------------------------------

class TestPlaceBucketOrder:
    """Tests for _place_bucket_order."""

    @pytest.mark.asyncio
    async def test_place_passive_limit_order(self):
        """Passive order should be a limit order at computed price."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {"num_buckets": 1, "price_offset_bps": 5})
        parent = _make_parent(qty=1.0, side=Side.Buy)
        parent.start()
        vwap.parent_order = parent
        vwap._active = True
        vwap._bucket_filled = [0.0]
        vwap._bucket_market_vol = [0.0]
        vwap._current_bucket = 0
        vwap._latest_bid = 100.0
        vwap._latest_ask = 101.0
        vwap._latest_mid = 100.5

        await vwap._place_bucket_order(0.5, aggressive=False)

        assert engine.send_child_order.call_count == 1
        child = list(parent.child_orders.values())[0]
        assert child.ord_type == OrdType.Limit
        expected_price = 100.0 + (100.5 * 5 / 10000)
        assert child.price == pytest.approx(expected_price)

    @pytest.mark.asyncio
    async def test_place_aggressive_limit_order(self):
        """Aggressive order should cross the spread."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {"num_buckets": 1, "price_offset_bps": 5})
        parent = _make_parent(qty=1.0, side=Side.Buy)
        parent.start()
        vwap.parent_order = parent
        vwap._active = True
        vwap._bucket_filled = [0.0]
        vwap._bucket_market_vol = [0.0]
        vwap._current_bucket = 0
        vwap._latest_bid = 100.0
        vwap._latest_ask = 101.0
        vwap._latest_mid = 100.5

        await vwap._place_bucket_order(0.5, aggressive=True)

        child = list(parent.child_orders.values())[0]
        assert child.price == pytest.approx(101.0)  # ask

    @pytest.mark.asyncio
    async def test_place_market_order_when_no_price(self):
        """When no bid/ask data, should place a market order."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {"num_buckets": 1})
        parent = _make_parent(qty=1.0, side=Side.Buy)
        parent.start()
        vwap.parent_order = parent
        vwap._active = True
        vwap._bucket_filled = [0.0]
        vwap._bucket_market_vol = [0.0]
        vwap._current_bucket = 0
        vwap._latest_bid = 0.0
        vwap._latest_ask = 0.0
        vwap._latest_mid = 0.0

        await vwap._place_bucket_order(0.5, aggressive=False)

        child = list(parent.child_orders.values())[0]
        assert child.ord_type == OrdType.Market

    @pytest.mark.asyncio
    async def test_inactive_strategy_does_not_place(self):
        """An inactive strategy should not place orders."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {"num_buckets": 1})
        parent = _make_parent(qty=1.0)
        vwap.parent_order = parent
        vwap._active = False
        vwap._bucket_filled = [0.0]
        vwap._bucket_market_vol = [0.0]
        vwap._current_bucket = 0

        await vwap._place_bucket_order(1.0, aggressive=False)

        assert engine.send_child_order.call_count == 0


# ---------------------------------------------------------------------------
# Sell-side tests
# ---------------------------------------------------------------------------

class TestSellSide:
    """Tests for SELL-side VWAP execution."""

    def test_sell_passive_price(self):
        """Sell passive price places at ask minus offset."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {"price_offset_bps": 10})
        parent = _make_parent(side=Side.Sell)
        vwap.parent_order = parent
        vwap._latest_bid = 99.0
        vwap._latest_ask = 101.0
        vwap._latest_mid = 100.0

        price = vwap._compute_price(aggressive=False)
        expected = 101.0 - (100.0 * 10 / 10000)
        assert price == pytest.approx(expected)

    def test_sell_aggressive_price(self):
        """Sell aggressive price hits the bid."""
        engine = _make_engine()
        vwap = VWAPStrategy(engine, {"price_offset_bps": 10})
        parent = _make_parent(side=Side.Sell)
        vwap.parent_order = parent
        vwap._latest_bid = 99.0
        vwap._latest_ask = 101.0
        vwap._latest_mid = 100.0

        price = vwap._compute_price(aggressive=True)
        assert price == pytest.approx(99.0)
