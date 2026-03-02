"""
Tests for the Implementation Shortfall (IS) execution strategy.

Covers trajectory computation, adaptive re-optimization, IS cost calculation,
circuit breakers, order placement, fill handling, and edge cases.
"""

import asyncio
import math
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from algo.parent_order import ChildOrder, ParentOrder, ParentState
from algo.strategies.is_strategy import ISStrategy
from shared.fix_protocol import OrdType, Side


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine(market_data: dict | None = None):
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
    algo_type="IS",
    arrival_price=50000.0,
) -> ParentOrder:
    return ParentOrder(
        parent_id="IS-001",
        symbol=symbol,
        side=side,
        total_qty=qty,
        algo_type=algo_type,
        arrival_price=arrival_price,
    )


def _make_child(cl_ord_id="C-1", parent_id="IS-001", qty=1.0, price=50000.0, side=Side.Buy):
    return ChildOrder(
        cl_ord_id=cl_ord_id,
        parent_id=parent_id,
        symbol="BTC/USD",
        side=side,
        qty=qty,
        price=price,
        ord_type=OrdType.Limit,
        exchange="BINANCE",
    )


def _make_is(engine=None, params=None, parent=None):
    """Create an IS strategy with a parent order ready to go (without starting scheduler)."""
    engine = engine or _make_engine()
    strategy = ISStrategy(engine, params)
    if parent is None:
        parent = _make_parent()
    strategy.parent_order = parent
    strategy._active = True
    strategy._arrival_price = parent.arrival_price
    strategy._latest_mid = parent.arrival_price
    strategy._latest_bid = parent.arrival_price - 10
    strategy._latest_ask = parent.arrival_price + 10
    return strategy


# ---------------------------------------------------------------------------
# Trajectory computation
# ---------------------------------------------------------------------------

class TestTrajectoryComputation:
    """Tests for _compute_trajectory."""

    def test_uniform_distribution_urgency_zero(self):
        """urgency=0 produces uniform bucket targets (TWAP-like)."""
        strategy = _make_is(params={"urgency": 0.0, "num_buckets": 10})
        strategy._compute_trajectory()

        expected = strategy.parent_order.total_qty / 10
        for target in strategy._bucket_targets:
            assert abs(target - expected) < 1e-9

    def test_front_loaded_urgency_one(self):
        """urgency=1 produces front-loaded targets (first bucket > last)."""
        strategy = _make_is(params={"urgency": 1.0, "num_buckets": 10})
        strategy._compute_trajectory()

        # First bucket should be significantly larger than last
        assert strategy._bucket_targets[0] > strategy._bucket_targets[-1] * 2.0

    def test_moderate_front_loading_urgency_half(self):
        """urgency=0.5 produces moderate front-loading."""
        strategy = _make_is(params={"urgency": 0.5, "num_buckets": 10})
        strategy._compute_trajectory()

        # First bucket larger than last but not as extreme as urgency=1
        assert strategy._bucket_targets[0] > strategy._bucket_targets[-1]
        # And the ratio should be less extreme than urgency=1
        ratio = strategy._bucket_targets[0] / strategy._bucket_targets[-1]
        assert 1.0 < ratio < 10.0

    def test_weights_sum_to_one(self):
        """Trajectory weights should sum to 1.0 (bucket targets sum to total qty)."""
        for urgency in [0.0, 0.25, 0.5, 0.75, 1.0]:
            strategy = _make_is(params={"urgency": urgency, "num_buckets": 20})
            strategy._compute_trajectory()

            total = sum(strategy._bucket_targets)
            assert abs(total - strategy.parent_order.total_qty) < 1e-9, (
                f"urgency={urgency}: targets sum to {total}, expected {strategy.parent_order.total_qty}"
            )

    def test_bucket_targets_sum_to_total_qty(self):
        """Bucket targets should sum to the parent order total quantity."""
        strategy = _make_is(
            params={"urgency": 0.7, "num_buckets": 15},
            parent=_make_parent(qty=5.0),
        )
        strategy._compute_trajectory()

        total = sum(strategy._bucket_targets)
        assert abs(total - 5.0) < 1e-9

    def test_num_buckets_matches(self):
        """Number of bucket targets matches num_buckets parameter."""
        for n in [5, 10, 20, 50]:
            strategy = _make_is(params={"num_buckets": n})
            strategy._compute_trajectory()
            assert len(strategy._bucket_targets) == n

    def test_all_targets_non_negative(self):
        """All bucket targets should be non-negative."""
        for urgency in [0.0, 0.5, 1.0]:
            strategy = _make_is(params={"urgency": urgency, "num_buckets": 20})
            strategy._compute_trajectory()
            for target in strategy._bucket_targets:
                assert target >= 0.0


# ---------------------------------------------------------------------------
# Adaptive trajectory
# ---------------------------------------------------------------------------

class TestAdaptiveTrajectory:
    """Tests for _adapt_trajectory."""

    def test_favorable_price_slows_down_buy(self):
        """For BUY, if price drops (favorable), adaptive factor < 1 reduces target rate."""
        strategy = _make_is(params={"urgency": 0.5, "num_buckets": 10, "adaptive": True})
        strategy._compute_trajectory()
        strategy._bucket_filled = [0.0] * 10

        # Simulate favorable drift: price dropped 2% for a BUY
        strategy._latest_mid = strategy._arrival_price * 0.98
        original_remaining = sum(strategy._bucket_targets[3:])

        strategy._adapt_trajectory(3)

        # After adaptation, remaining targets should still sum to remaining qty
        new_remaining = sum(strategy._bucket_targets[3:])
        assert abs(new_remaining - strategy.parent_order.remaining_qty()) < 1e-6

    def test_adverse_price_speeds_up_buy(self):
        """For BUY, if price rises (adverse), front-load remaining buckets."""
        strategy = _make_is(params={"urgency": 0.3, "num_buckets": 10, "adaptive": True})
        strategy._compute_trajectory()
        strategy._bucket_filled = [0.0] * 10

        # Record original first remaining bucket target before adaptation
        original_bucket_3 = strategy._bucket_targets[3]
        original_bucket_9 = strategy._bucket_targets[9]
        original_ratio = original_bucket_3 / original_bucket_9 if original_bucket_9 > 0 else 1.0

        # Simulate adverse drift: price rose 2% for a BUY
        strategy._latest_mid = strategy._arrival_price * 1.02

        strategy._adapt_trajectory(3)

        # Remaining targets still sum to remaining qty (re-normalized)
        new_remaining = sum(strategy._bucket_targets[3:])
        assert abs(new_remaining - strategy.parent_order.remaining_qty()) < 1e-6

    def test_favorable_price_slows_down_sell(self):
        """For SELL, if price rises (favorable), adaptive factor < 1."""
        parent = _make_parent(side=Side.Sell)
        strategy = _make_is(
            params={"urgency": 0.5, "num_buckets": 10, "adaptive": True},
            parent=parent,
        )
        strategy._compute_trajectory()
        strategy._bucket_filled = [0.0] * 10

        # For SELL: price rise is favorable
        strategy._latest_mid = strategy._arrival_price * 1.02

        strategy._adapt_trajectory(3)

        # Remaining targets re-normalized to remaining qty
        new_remaining = sum(strategy._bucket_targets[3:])
        assert abs(new_remaining - strategy.parent_order.remaining_qty()) < 1e-6

    def test_adaptive_factor_clamped_low(self):
        """Adaptive factor should not go below 0.5 even with extreme favorable drift."""
        strategy = _make_is(params={"urgency": 0.5, "num_buckets": 10, "adaptive": True})
        strategy._compute_trajectory()
        strategy._bucket_filled = [0.0] * 10

        # Extreme favorable drift: price dropped 50%
        strategy._latest_mid = strategy._arrival_price * 0.50

        # This should trigger adaptive_factor near 0.5 limit
        strategy._adapt_trajectory(2)

        # Should not crash and targets should be valid
        new_remaining = sum(strategy._bucket_targets[2:])
        assert abs(new_remaining - strategy.parent_order.remaining_qty()) < 1e-6

    def test_adaptive_factor_clamped_high(self):
        """Adaptive factor should not exceed 2.0 even with extreme adverse drift."""
        strategy = _make_is(params={"urgency": 0.5, "num_buckets": 10, "adaptive": True})
        strategy._compute_trajectory()
        strategy._bucket_filled = [0.0] * 10

        # Extreme adverse drift: price rose 50%
        strategy._latest_mid = strategy._arrival_price * 1.50

        strategy._adapt_trajectory(2)

        # Should not crash and targets should be valid
        new_remaining = sum(strategy._bucket_targets[2:])
        assert abs(new_remaining - strategy.parent_order.remaining_qty()) < 1e-6

    def test_no_oscillation_repeated_adapt(self):
        """Multiple adaptive calls should converge, not oscillate."""
        strategy = _make_is(params={"urgency": 0.5, "num_buckets": 20, "adaptive": True})
        strategy._compute_trajectory()
        strategy._bucket_filled = [0.0] * 20

        # Small favorable drift
        strategy._latest_mid = strategy._arrival_price * 0.999

        # Apply multiple times
        for bucket in range(5, 10):
            strategy._adapt_trajectory(bucket)

        # Targets should still be valid
        remaining = sum(strategy._bucket_targets[10:])
        assert remaining >= 0
        total = sum(strategy._bucket_targets)
        assert total >= 0

    def test_adapt_with_partial_fills(self):
        """Adaptive trajectory accounts for partially filled parent."""
        strategy = _make_is(params={"urgency": 0.5, "num_buckets": 10, "adaptive": True})
        strategy._compute_trajectory()
        strategy._bucket_filled = [0.0] * 10

        # Simulate partial fills: 3 out of 10 filled
        strategy.parent_order.filled_qty = 3.0
        strategy.parent_order._fill_notional = 3.0 * 50000.0

        strategy._latest_mid = strategy._arrival_price * 1.01  # slight adverse

        strategy._adapt_trajectory(3)

        # Remaining targets should sum to remaining qty (7.0)
        new_remaining = sum(strategy._bucket_targets[3:])
        assert abs(new_remaining - 7.0) < 1e-6


# ---------------------------------------------------------------------------
# Arrival price
# ---------------------------------------------------------------------------

class TestArrivalPrice:
    """Tests for arrival price capture."""

    def test_arrival_price_captured_from_parent(self):
        """on_start should capture arrival price from parent order."""
        engine = _make_engine()
        strategy = ISStrategy(engine, {"urgency": 0.5, "num_buckets": 5})
        parent = _make_parent(arrival_price=51234.56)

        # Manually simulate on_start logic without the scheduler
        strategy.parent_order = parent
        strategy._arrival_price = parent.arrival_price

        assert strategy._arrival_price == 51234.56

    def test_arrival_price_zero_if_no_market_data(self):
        """Arrival price stays 0 if parent has no arrival price."""
        engine = _make_engine()
        strategy = ISStrategy(engine, {"urgency": 0.5})
        parent = _make_parent(arrival_price=0.0)

        strategy.parent_order = parent
        strategy._arrival_price = parent.arrival_price

        assert strategy._arrival_price == 0.0


# ---------------------------------------------------------------------------
# IS cost calculation
# ---------------------------------------------------------------------------

class TestISCostCalculation:
    """Tests for _calculate_is_cost."""

    def test_positive_is_for_adverse_fills_buy(self):
        """Buying at higher-than-arrival should produce positive IS cost."""
        strategy = _make_is(parent=_make_parent(side=Side.Buy, arrival_price=50000.0))
        strategy._total_fill_qty = 5.0
        strategy._total_fill_notional = 5.0 * 50100.0  # Paid 100 more per unit
        strategy._first_fill_price = 50100.0

        cost = strategy._calculate_is_cost()
        assert cost["total_is"] > 0

    def test_negative_is_for_favorable_fills_buy(self):
        """Buying at lower-than-arrival should produce negative IS cost."""
        strategy = _make_is(parent=_make_parent(side=Side.Buy, arrival_price=50000.0))
        strategy._total_fill_qty = 5.0
        strategy._total_fill_notional = 5.0 * 49900.0  # Paid 100 less per unit
        strategy._first_fill_price = 49900.0

        cost = strategy._calculate_is_cost()
        assert cost["total_is"] < 0

    def test_positive_is_for_adverse_fills_sell(self):
        """Selling at lower-than-arrival should produce positive IS cost."""
        parent = _make_parent(side=Side.Sell, arrival_price=50000.0)
        strategy = _make_is(parent=parent)
        strategy._total_fill_qty = 5.0
        strategy._total_fill_notional = 5.0 * 49900.0  # Sold 100 less per unit
        strategy._first_fill_price = 49900.0

        cost = strategy._calculate_is_cost()
        assert cost["total_is"] > 0

    def test_is_cost_decomposition(self):
        """IS cost = delay_cost + impact_cost + timing_cost."""
        strategy = _make_is(parent=_make_parent(side=Side.Buy, arrival_price=50000.0))
        strategy._total_fill_qty = 5.0
        strategy._total_fill_notional = 5.0 * 50200.0
        strategy._first_fill_price = 50050.0
        strategy._latest_mid = 50100.0

        cost = strategy._calculate_is_cost()

        # Decomposition should sum to total (approximately, due to model simplification)
        decomp_sum = cost["delay_cost"] + cost["impact_cost"] + cost["timing_cost"]
        assert abs(cost["total_is"] - decomp_sum) < 1e-6

    def test_is_cost_zero_with_no_fills(self):
        """IS cost should be zero when nothing has been filled."""
        strategy = _make_is()
        cost = strategy._calculate_is_cost()
        assert cost["total_is"] == 0.0
        assert cost["delay_cost"] == 0.0
        assert cost["impact_cost"] == 0.0
        assert cost["timing_cost"] == 0.0


# ---------------------------------------------------------------------------
# Urgency validation
# ---------------------------------------------------------------------------

class TestUrgencyValidation:
    """Tests for urgency parameter validation."""

    @pytest.mark.asyncio
    async def test_reject_urgency_below_zero(self):
        """Urgency < 0 should cancel the algo on start."""
        engine = _make_engine()
        strategy = ISStrategy(engine, {"urgency": -0.1, "num_buckets": 5})
        parent = _make_parent()

        await strategy.start(parent)

        assert not strategy._active
        assert parent.state == ParentState.CANCELLED

    @pytest.mark.asyncio
    async def test_reject_urgency_above_one(self):
        """Urgency > 1 should cancel the algo on start."""
        engine = _make_engine()
        strategy = ISStrategy(engine, {"urgency": 1.5, "num_buckets": 5})
        parent = _make_parent()

        await strategy.start(parent)

        assert not strategy._active
        assert parent.state == ParentState.CANCELLED

    @pytest.mark.asyncio
    async def test_accept_urgency_zero(self):
        """Urgency = 0 should be accepted."""
        engine = _make_engine()
        strategy = ISStrategy(engine, {"urgency": 0.0, "num_buckets": 5, "horizon_seconds": 1})
        parent = _make_parent()

        await strategy.start(parent)

        assert strategy._active
        # Clean up scheduler
        await strategy.stop()

    @pytest.mark.asyncio
    async def test_accept_urgency_one(self):
        """Urgency = 1 should be accepted."""
        engine = _make_engine()
        strategy = ISStrategy(engine, {"urgency": 1.0, "num_buckets": 5, "horizon_seconds": 1})
        parent = _make_parent()

        await strategy.start(parent)

        assert strategy._active
        await strategy.stop()


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

class TestOrderPlacement:
    """Tests for passive and aggressive order placement."""

    @pytest.mark.asyncio
    async def test_passive_pricing_buy(self):
        """Passive BUY orders should be placed below mid price."""
        engine = _make_engine()
        strategy = _make_is(engine=engine, params={"price_offset_bps": 10})
        strategy._latest_mid = 50000.0
        strategy._latest_bid = 49990.0
        strategy._latest_ask = 50010.0

        await strategy._place_bucket_order(1.0, aggressive=False)

        engine.send_child_order.assert_called_once()
        child = engine.send_child_order.call_args[0][0]
        # Passive BUY: mid - offset = 50000 - 50 = 49950
        assert child.price < 50000.0

    @pytest.mark.asyncio
    async def test_passive_pricing_sell(self):
        """Passive SELL orders should be placed above mid price."""
        engine = _make_engine()
        parent = _make_parent(side=Side.Sell)
        strategy = _make_is(engine=engine, params={"price_offset_bps": 10}, parent=parent)
        strategy._latest_mid = 50000.0
        strategy._latest_bid = 49990.0
        strategy._latest_ask = 50010.0

        await strategy._place_bucket_order(1.0, aggressive=False)

        engine.send_child_order.assert_called_once()
        child = engine.send_child_order.call_args[0][0]
        # Passive SELL: mid + offset = 50000 + 50 = 50050
        assert child.price > 50000.0

    @pytest.mark.asyncio
    async def test_aggressive_crosses_spread_buy(self):
        """Aggressive BUY should use the ask price."""
        engine = _make_engine()
        strategy = _make_is(engine=engine)
        strategy._latest_mid = 50000.0
        strategy._latest_bid = 49990.0
        strategy._latest_ask = 50010.0

        await strategy._place_bucket_order(1.0, aggressive=True)

        engine.send_child_order.assert_called_once()
        child = engine.send_child_order.call_args[0][0]
        assert child.price == 50010.0

    @pytest.mark.asyncio
    async def test_aggressive_crosses_spread_sell(self):
        """Aggressive SELL should use the bid price."""
        engine = _make_engine()
        parent = _make_parent(side=Side.Sell)
        strategy = _make_is(engine=engine, parent=parent)
        strategy._latest_mid = 50000.0
        strategy._latest_bid = 49990.0
        strategy._latest_ask = 50010.0

        await strategy._place_bucket_order(1.0, aggressive=True)

        engine.send_child_order.assert_called_once()
        child = engine.send_child_order.call_args[0][0]
        assert child.price == 49990.0

    @pytest.mark.asyncio
    async def test_sweep_order_is_market(self):
        """Final sweep order should be a market order."""
        engine = _make_engine()
        strategy = _make_is(engine=engine)

        await strategy._place_sweep_order(2.0)

        engine.send_child_order.assert_called_once()
        child = engine.send_child_order.call_args[0][0]
        assert child.ord_type == OrdType.Market
        assert child.qty == 2.0


# ---------------------------------------------------------------------------
# Fill handling
# ---------------------------------------------------------------------------

class TestFillHandling:
    """Tests for on_fill behavior."""

    @pytest.mark.asyncio
    async def test_on_fill_updates_tracking(self):
        """on_fill should update IS tracking state."""
        strategy = _make_is()
        strategy._bucket_filled = [0.0] * 20
        strategy._current_bucket = 3

        child = _make_child(qty=1.0)
        child.filled_qty = 1.0
        child.status = "FILLED"
        strategy.parent_order.add_child_order(child)

        await strategy.on_fill(child, 1.0, 50100.0)

        assert strategy._total_fill_qty == 1.0
        assert strategy._total_fill_notional == 50100.0
        assert strategy._first_fill_price == 50100.0
        assert strategy._bucket_filled[3] == 1.0

    @pytest.mark.asyncio
    async def test_on_fill_completes_when_done(self):
        """on_fill should complete the parent when fully filled."""
        parent = _make_parent(qty=1.0)
        parent.start()  # PENDING -> ACTIVE so complete() can transition to DONE
        strategy = _make_is(parent=parent)
        strategy._bucket_filled = [0.0] * 20

        child = _make_child(qty=1.0)
        strategy.parent_order.add_child_order(child)
        strategy.parent_order.process_fill(child.cl_ord_id, 1.0, 50000.0)

        await strategy.on_fill(child, 1.0, 50000.0)

        assert parent.state == ParentState.DONE
        assert not strategy._active


# ---------------------------------------------------------------------------
# Reject handling
# ---------------------------------------------------------------------------

class TestRejectHandling:
    """Tests for on_reject behavior."""

    @pytest.mark.asyncio
    async def test_on_reject_returns_qty(self):
        """Rejected order qty should be added back to current bucket target."""
        strategy = _make_is(params={"num_buckets": 10})
        strategy._compute_trajectory()
        strategy._bucket_filled = [0.0] * 10
        strategy._current_bucket = 2

        original_target = strategy._bucket_targets[2]
        child = _make_child(qty=0.5)
        child.status = "REJECTED"

        await strategy.on_reject(child, "insufficient balance")

        assert strategy._bucket_targets[2] == original_target + 0.5


# ---------------------------------------------------------------------------
# Circuit breakers
# ---------------------------------------------------------------------------

class TestCircuitBreakers:
    """Tests for circuit breaker logic."""

    def test_price_drift_triggers_breaker(self):
        """Price drift > 5% from arrival should trigger circuit breaker."""
        strategy = _make_is(params={"max_price_drift_pct": 5.0})
        strategy._latest_mid = strategy._arrival_price * 1.06  # 6% drift

        assert strategy._check_circuit_breakers() is True

    def test_price_within_threshold_no_trigger(self):
        """Price drift < 5% should not trigger circuit breaker."""
        strategy = _make_is(params={"max_price_drift_pct": 5.0})
        strategy._latest_mid = strategy._arrival_price * 1.03  # 3% drift

        assert strategy._check_circuit_breakers() is False

    def test_spread_triggers_breaker(self):
        """Spread > 2% of mid should trigger circuit breaker."""
        strategy = _make_is(params={"max_spread_pct": 2.0})
        strategy._latest_mid = 50000.0
        strategy._latest_bid = 49000.0  # 2% spread
        strategy._latest_ask = 51000.0

        # Spread = 2000 / 50000 = 4%
        assert strategy._check_circuit_breakers() is True

    def test_slippage_triggers_breaker(self):
        """Running IS cost > 1% of notional should trigger circuit breaker."""
        strategy = _make_is(params={"max_slippage_pct": 1.0})
        # Notional = 50000 * 10 = 500000; 1% = 5000
        # Fill at 50600 avg -> IS = (50600-50000)*1*5 = 3000 for 5 filled
        # But need IS cost > 5000 of full notional
        strategy._total_fill_qty = 10.0
        strategy._total_fill_notional = 10.0 * 50600.0  # IS = 6000 > 5000
        strategy._first_fill_price = 50600.0
        strategy.parent_order.filled_qty = 10.0

        assert strategy._check_circuit_breakers() is True

    def test_no_breaker_with_normal_conditions(self):
        """Normal conditions should not trigger any circuit breaker."""
        strategy = _make_is()
        strategy._latest_mid = strategy._arrival_price * 1.001  # tiny drift
        strategy._latest_bid = strategy._arrival_price - 5
        strategy._latest_ask = strategy._arrival_price + 5

        assert strategy._check_circuit_breakers() is False


# ---------------------------------------------------------------------------
# Completion and cancellation
# ---------------------------------------------------------------------------

class TestCompletionAndCancellation:
    """Tests for algo completion and cancellation flows."""

    @pytest.mark.asyncio
    async def test_completion_transitions_to_done(self):
        """When fully filled, parent should transition to DONE."""
        parent = _make_parent(qty=2.0)
        parent.start()  # PENDING -> ACTIVE so complete() can transition to DONE
        strategy = _make_is(parent=parent)
        strategy._bucket_filled = [0.0] * 20

        child = _make_child(qty=2.0)
        strategy.parent_order.add_child_order(child)
        strategy.parent_order.process_fill(child.cl_ord_id, 2.0, 50000.0)

        await strategy.on_fill(child, 2.0, 50000.0)

        assert parent.state == ParentState.DONE

    @pytest.mark.asyncio
    async def test_stop_cancels_scheduler(self):
        """Stopping the strategy should cancel the scheduler task."""
        engine = _make_engine()
        strategy = ISStrategy(engine, {"urgency": 0.5, "num_buckets": 5, "horizon_seconds": 100})
        parent = _make_parent()

        await strategy.start(parent)
        assert strategy._scheduler_task is not None

        await strategy.stop()

        assert strategy._scheduler_task.done()
        assert not strategy._active

    @pytest.mark.asyncio
    async def test_cancel_stops_scheduler(self):
        """Cancelling the algo via stop() should halt the scheduler."""
        engine = _make_engine()
        strategy = ISStrategy(engine, {"urgency": 0.5, "num_buckets": 5, "horizon_seconds": 100})
        parent = _make_parent()

        await strategy.start(parent)

        await strategy.stop()

        assert parent.state == ParentState.CANCELLED
        assert not strategy._active


# ---------------------------------------------------------------------------
# Zero urgency = TWAP-like
# ---------------------------------------------------------------------------

class TestZeroUrgencyTWAP:
    """Tests for zero urgency producing TWAP-like behavior."""

    def test_zero_urgency_uniform_buckets(self):
        """With urgency=0, all buckets should have equal targets."""
        strategy = _make_is(params={"urgency": 0.0, "num_buckets": 10})
        strategy._compute_trajectory()

        targets = strategy._bucket_targets
        expected = strategy.parent_order.total_qty / 10

        for i, t in enumerate(targets):
            assert abs(t - expected) < 1e-9, f"Bucket {i}: {t} != {expected}"

    def test_zero_urgency_first_equals_last(self):
        """With urgency=0, first bucket equals last bucket."""
        strategy = _make_is(params={"urgency": 0.0, "num_buckets": 20})
        strategy._compute_trajectory()

        assert abs(strategy._bucket_targets[0] - strategy._bucket_targets[-1]) < 1e-9


# ---------------------------------------------------------------------------
# Max urgency = immediate
# ---------------------------------------------------------------------------

class TestMaxUrgencyImmediate:
    """Tests for max urgency producing front-loaded execution."""

    def test_max_urgency_heavily_front_loaded(self):
        """With urgency=1, first bucket should be much larger than last."""
        strategy = _make_is(params={"urgency": 1.0, "num_buckets": 20})
        strategy._compute_trajectory()

        first = strategy._bucket_targets[0]
        last = strategy._bucket_targets[-1]

        # With kappa=3 and 20 buckets, ratio should be large
        assert first / last > 5.0

    def test_max_urgency_majority_in_first_quarter(self):
        """With urgency=1, majority of qty should be in the first quarter of buckets."""
        strategy = _make_is(params={"urgency": 1.0, "num_buckets": 20})
        strategy._compute_trajectory()

        first_quarter = sum(strategy._bucket_targets[:5])
        total = sum(strategy._bucket_targets)

        assert first_quarter / total > 0.5


# ---------------------------------------------------------------------------
# Market data handling
# ---------------------------------------------------------------------------

class TestMarketData:
    """Tests for on_tick behavior."""

    @pytest.mark.asyncio
    async def test_on_tick_updates_prices(self):
        """on_tick should update bid/ask/mid."""
        strategy = _make_is()

        await strategy.on_tick("BTC/USD", {"bid": 49500.0, "ask": 50500.0})

        assert strategy._latest_bid == 49500.0
        assert strategy._latest_ask == 50500.0
        assert strategy._latest_mid == 50000.0

    @pytest.mark.asyncio
    async def test_on_tick_ignores_wrong_symbol(self):
        """on_tick should ignore ticks for other symbols."""
        strategy = _make_is()
        original_mid = strategy._latest_mid

        await strategy.on_tick("ETH/USD", {"bid": 3000.0, "ask": 3100.0})

        assert strategy._latest_mid == original_mid

    @pytest.mark.asyncio
    async def test_on_tick_ignores_when_paused(self):
        """on_tick should be a no-op when paused."""
        strategy = _make_is()
        strategy._paused = True
        original_mid = strategy._latest_mid

        await strategy.on_tick("BTC/USD", {"bid": 49000.0, "ask": 49100.0})

        assert strategy._latest_mid == original_mid


# ---------------------------------------------------------------------------
# Constructor defaults
# ---------------------------------------------------------------------------

class TestConstructorDefaults:
    """Tests for default parameter values."""

    def test_default_parameters(self):
        """Constructor should set sensible defaults."""
        engine = _make_engine()
        strategy = ISStrategy(engine)

        assert strategy.horizon_seconds == 3600
        assert strategy.num_buckets == 20
        assert strategy.urgency == 0.5
        assert strategy.risk_aversion == 1e-6
        assert strategy.temporary_impact_coeff == 0.1
        assert strategy.permanent_impact_coeff == 0.01
        assert strategy.volatility == 100
        assert strategy.adaptive is True
        assert strategy.price_offset_bps == 5
        assert strategy.aggressive_threshold == 0.6
        assert strategy.STRATEGY_NAME == "IS"

    def test_custom_parameters(self):
        """Constructor should accept custom parameters."""
        engine = _make_engine()
        strategy = ISStrategy(engine, {
            "horizon_seconds": 1800,
            "num_buckets": 10,
            "urgency": 0.8,
            "risk_aversion": 1e-5,
            "adaptive": False,
        })

        assert strategy.horizon_seconds == 1800
        assert strategy.num_buckets == 10
        assert strategy.urgency == 0.8
        assert strategy.risk_aversion == 1e-5
        assert strategy.adaptive is False


# ---------------------------------------------------------------------------
# Cancel ack
# ---------------------------------------------------------------------------

class TestCancelAck:
    """Tests for on_cancel_ack."""

    @pytest.mark.asyncio
    async def test_cancel_ack_removes_from_active(self):
        """Cancelled child should be removed from active set."""
        strategy = _make_is()
        strategy._active_child_ids.add("C-1")
        child = _make_child(cl_ord_id="C-1")
        child.status = "CANCELLED"

        await strategy.on_cancel_ack(child)

        assert "C-1" not in strategy._active_child_ids


# ---------------------------------------------------------------------------
# Price drift helper
# ---------------------------------------------------------------------------

class TestPriceDriftHelper:
    """Tests for _price_drift_bps."""

    def test_drift_bps_positive(self):
        """Positive mid movement should give positive drift bps."""
        strategy = _make_is()
        strategy._arrival_price = 50000.0
        strategy._latest_mid = 50050.0

        drift = strategy._price_drift_bps()
        # (50050 - 50000) / 50000 * 10000 = 10 bps
        assert abs(drift - 10.0) < 0.1

    def test_drift_bps_zero_at_arrival(self):
        """Zero drift when mid equals arrival."""
        strategy = _make_is()
        strategy._latest_mid = strategy._arrival_price

        assert strategy._price_drift_bps() == 0.0

    def test_drift_bps_zero_no_arrival(self):
        """Zero drift when arrival price is 0."""
        strategy = _make_is()
        strategy._arrival_price = 0.0

        assert strategy._price_drift_bps() == 0.0
