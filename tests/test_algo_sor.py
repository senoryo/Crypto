"""
Tests for the Smart Order Router (SOR) execution strategy.

Covers venue scoring, routing modes (best/split/spray), fee scoring,
price improvement scoring, latency scoring, rejection handling,
anti-gaming randomization, minimum order sizes, and quantity accounting.
"""

import asyncio
import random
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from algo.parent_order import ChildOrder, ParentOrder, ParentState
from algo.strategies.sor import (
    DEFAULT_FEE_SCHEDULE,
    DEFAULT_LATENCY_ESTIMATES,
    DEFAULT_MIN_ORDER_SIZE,
    RouteAllocation,
    SmartOrderRouter,
    VenueScore,
)
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
    qty=1.0,
    algo_type="SOR",
    params=None,
) -> ParentOrder:
    return ParentOrder(
        parent_id="P-001",
        symbol=symbol,
        side=side,
        total_qty=qty,
        algo_type=algo_type,
        params=params or {},
    )


def _venue_tick(bid, ask, bid_size=10.0, ask_size=10.0, exchange=None):
    d = {"bid": bid, "ask": ask, "bid_size": bid_size, "ask_size": ask_size}
    if exchange:
        d["exchange"] = exchange
    return d


# ---------------------------------------------------------------------------
# Venue scoring
# ---------------------------------------------------------------------------

class TestVenueScoring:
    """Tests for the venue scoring matrix."""

    def test_score_venues_buy_lowest_ask_wins(self):
        """For BUY orders, the venue with the lowest ask gets the best price score."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {"randomize_pct": 0, "score_noise": 0})
        venue_data = {
            "BINANCE": {"bid": 99, "ask": 100, "bid_size": 10, "ask_size": 10},
            "KRAKEN":  {"bid": 99, "ask": 101, "bid_size": 10, "ask_size": 10},
        }
        scores = sor._score_venues(venue_data, "BTC/USD", Side.Buy)
        assert len(scores) == 2
        # BINANCE has lower ask, should have higher price score
        binance = next(s for s in scores if s.exchange == "BINANCE")
        kraken = next(s for s in scores if s.exchange == "KRAKEN")
        assert binance.price_score > kraken.price_score

    def test_score_venues_sell_highest_bid_wins(self):
        """For SELL orders, the venue with the highest bid gets the best price score."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {"randomize_pct": 0, "score_noise": 0})
        venue_data = {
            "BINANCE": {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 10},
            "KRAKEN":  {"bid": 99,  "ask": 101, "bid_size": 10, "ask_size": 10},
        }
        scores = sor._score_venues(venue_data, "BTC/USD", Side.Sell)
        binance = next(s for s in scores if s.exchange == "BINANCE")
        kraken = next(s for s in scores if s.exchange == "KRAKEN")
        assert binance.price_score > kraken.price_score

    def test_score_venues_larger_size_wins(self):
        """Venues with more available liquidity get higher size scores."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {"randomize_pct": 0, "score_noise": 0})
        venue_data = {
            "BINANCE": {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 50},
            "KRAKEN":  {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 10},
        }
        scores = sor._score_venues(venue_data, "BTC/USD", Side.Buy)
        binance = next(s for s in scores if s.exchange == "BINANCE")
        kraken = next(s for s in scores if s.exchange == "KRAKEN")
        assert binance.size_score > kraken.size_score

    def test_score_venues_lower_fee_wins(self):
        """Venues with lower taker fees get higher fee scores."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {
            "randomize_pct": 0,
            "score_noise": 0,
            "fee_tiers": {
                "BINANCE": {"taker": 0.001},
                "HTX":     {"taker": 0.005},
            },
        })
        venue_data = {
            "BINANCE": {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 10},
            "HTX":     {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 10},
        }
        scores = sor._score_venues(venue_data, "BTC/USD", Side.Buy)
        binance = next(s for s in scores if s.exchange == "BINANCE")
        htx = next(s for s in scores if s.exchange == "HTX")
        assert binance.fee_score > htx.fee_score

    def test_score_venues_lower_latency_wins(self):
        """Venues with lower latency estimates get higher latency scores."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {
            "randomize_pct": 0,
            "score_noise": 0,
            "latency_estimates": {"KRAKEN": 0.05, "HTX": 0.50},
        })
        venue_data = {
            "KRAKEN": {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 10},
            "HTX":    {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 10},
        }
        scores = sor._score_venues(venue_data, "BTC/USD", Side.Buy)
        kraken = next(s for s in scores if s.exchange == "KRAKEN")
        htx = next(s for s in scores if s.exchange == "HTX")
        assert kraken.latency_score > htx.latency_score

    def test_score_venues_composite_weighted(self):
        """Composite score respects the weight configuration."""
        engine = _make_engine()
        # Only price matters
        sor = SmartOrderRouter(engine, {
            "randomize_pct": 0,
            "score_noise": 0,
            "score_weights": {"w_price": 1.0, "w_size": 0, "w_fee": 0, "w_latency": 0},
        })
        venue_data = {
            "BINANCE": {"bid": 100, "ask": 100, "bid_size": 1, "ask_size": 1},
            "KRAKEN":  {"bid": 100, "ask": 105, "bid_size": 100, "ask_size": 100},
        }
        scores = sor._score_venues(venue_data, "BTC/USD", Side.Buy)
        # BINANCE has the better ask=100, so should be first
        assert scores[0].exchange == "BINANCE"

    def test_score_venues_empty_data(self):
        """Empty venue data returns empty scores."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine)
        assert sor._score_venues({}, "BTC/USD", Side.Buy) == []

    def test_score_venues_all_zero_prices(self):
        """All-zero prices returns empty scores."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {"randomize_pct": 0, "score_noise": 0})
        venue_data = {
            "BINANCE": {"bid": 0, "ask": 0, "bid_size": 10, "ask_size": 10},
        }
        assert sor._score_venues(venue_data, "BTC/USD", Side.Buy) == []


# ---------------------------------------------------------------------------
# Best-venue routing
# ---------------------------------------------------------------------------

class TestBestVenueRouting:
    """Tests for best-venue routing mode (default)."""

    def test_best_mode_selects_highest_scored(self):
        """Best mode routes 100% to the highest scored venue."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {
            "routing_mode": "best",
            "randomize_pct": 0,
            "score_noise": 0,
        })
        sor._venue_market_data = {
            "OKX":     {"bid": 100, "ask": 99,  "bid_size": 50, "ask_size": 50},
            "BINANCE": {"bid": 100, "ask": 100, "bid_size": 50, "ask_size": 50},
        }
        allocs = sor.route_order("BTC/USD", Side.Buy, 1.0)
        assert len(allocs) == 1
        assert allocs[0].exchange == "OKX"  # lower ask
        assert allocs[0].qty == 1.0

    def test_best_mode_single_venue(self):
        """With only one venue, route everything there."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {"routing_mode": "best", "randomize_pct": 0, "score_noise": 0})
        sor._venue_market_data = {
            "BINANCE": {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 10},
        }
        allocs = sor.route_order("BTC/USD", Side.Buy, 5.0)
        assert len(allocs) == 1
        assert allocs[0].exchange == "BINANCE"
        assert allocs[0].qty == 5.0


# ---------------------------------------------------------------------------
# Split routing
# ---------------------------------------------------------------------------

class TestSplitRouting:
    """Tests for split routing mode."""

    def test_split_single_venue_sufficient(self):
        """If best venue has enough liquidity, route everything there even in split mode."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {
            "routing_mode": "split",
            "randomize_pct": 0,
            "score_noise": 0,
        })
        sor._venue_market_data = {
            "BINANCE": {"bid": 100, "ask": 101, "bid_size": 100, "ask_size": 100},
            "KRAKEN":  {"bid": 100, "ask": 102, "bid_size": 100, "ask_size": 100},
        }
        allocs = sor.route_order("BTC/USD", Side.Buy, 5.0)
        assert len(allocs) == 1
        assert allocs[0].qty == 5.0

    def test_split_divides_across_venues(self):
        """When order exceeds best venue liquidity, split across multiple venues."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {
            "routing_mode": "split",
            "max_venues": 3,
            "randomize_pct": 0,
            "score_noise": 0,
        })
        sor._venue_market_data = {
            "BINANCE": {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 3},
            "KRAKEN":  {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 3},
            "OKX":     {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 4},
        }
        allocs = sor.route_order("BTC/USD", Side.Buy, 10.0)
        # Quantity should be fully accounted for
        total = sum(a.qty for a in allocs)
        assert abs(total - 10.0) < 1e-9

    def test_split_respects_max_venues(self):
        """Split mode should not exceed max_venues."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {
            "routing_mode": "split",
            "max_venues": 2,
            "randomize_pct": 0,
            "score_noise": 0,
        })
        sor._venue_market_data = {
            "BINANCE":  {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 2},
            "KRAKEN":   {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 2},
            "OKX":      {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 2},
            "BITFINEX": {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 2},
        }
        allocs = sor.route_order("BTC/USD", Side.Buy, 10.0)
        assert len(allocs) <= 2


# ---------------------------------------------------------------------------
# Spray-and-cancel
# ---------------------------------------------------------------------------

class TestSprayAndCancel:
    """Tests for spray-and-cancel routing mode."""

    @pytest.mark.asyncio
    async def test_spray_sends_to_multiple_venues(self):
        """Spray mode sends orders to top N venues simultaneously."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {
            "routing_mode": "spray",
            "max_venues": 3,
            "randomize_pct": 0,
            "score_noise": 0,
        })
        parent = _make_parent(qty=1.0)
        sor._venue_market_data = {
            "BINANCE": {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 10},
            "KRAKEN":  {"bid": 100, "ask": 102, "bid_size": 10, "ask_size": 10},
            "OKX":     {"bid": 100, "ask": 103, "bid_size": 10, "ask_size": 10},
        }
        await sor.start(parent)
        # Should have submitted 3 child orders (one per venue)
        assert engine.send_child_order.call_count == 3

    @pytest.mark.asyncio
    async def test_spray_cancel_on_first_fill(self):
        """After first fill in a spray group, unfilled siblings are cancelled."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {
            "routing_mode": "spray",
            "max_venues": 3,
            "randomize_pct": 0,
            "score_noise": 0,
        })
        parent = _make_parent(qty=1.0)
        sor._venue_market_data = {
            "BINANCE": {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 10},
            "KRAKEN":  {"bid": 100, "ask": 102, "bid_size": 10, "ask_size": 10},
            "OKX":     {"bid": 100, "ask": 103, "bid_size": 10, "ask_size": 10},
        }
        await sor.start(parent)

        # Get the child orders created
        children = list(parent.child_orders.values())
        assert len(children) == 3

        # Simulate fill on the first child
        first_child = children[0]
        parent.process_fill(first_child.cl_ord_id, 1.0, 101.0)
        await sor.on_fill(first_child, 1.0, 101.0)

        # The other two should have been cancelled via the engine
        assert engine.cancel_child_order.call_count == 2

    @pytest.mark.asyncio
    async def test_spray_tracks_groups(self):
        """Spray mode correctly tracks which children belong to which spray group."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {
            "routing_mode": "spray",
            "max_venues": 2,
            "randomize_pct": 0,
            "score_noise": 0,
        })
        parent = _make_parent(qty=1.0)
        sor._venue_market_data = {
            "BINANCE": {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 10},
            "KRAKEN":  {"bid": 100, "ask": 102, "bid_size": 10, "ask_size": 10},
        }
        await sor.start(parent)

        assert len(sor._spray_groups) == 1
        group_id = list(sor._spray_groups.keys())[0]
        assert len(sor._spray_groups[group_id]) == 2
        for cl_id in sor._spray_groups[group_id]:
            assert sor._child_spray_group[cl_id] == group_id


# ---------------------------------------------------------------------------
# Fee scoring
# ---------------------------------------------------------------------------

class TestFeeScoring:
    """Tests for fee-based scoring."""

    def test_lower_fee_produces_higher_score(self):
        """OKX with 0.08% fee should score higher than HTX with 0.20% fee."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {
            "randomize_pct": 0,
            "score_noise": 0,
            "fee_tiers": {
                "OKX": {"taker": 0.0008},
                "HTX": {"taker": 0.0020},
            },
        })
        venue_data = {
            "OKX": {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 10},
            "HTX": {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 10},
        }
        scores = sor._score_venues(venue_data, "BTC/USD", Side.Buy)
        okx = next(s for s in scores if s.exchange == "OKX")
        htx = next(s for s in scores if s.exchange == "HTX")
        assert okx.fee_score == 1.0  # cheapest
        assert htx.fee_score == 0.0  # most expensive
        assert okx.composite > htx.composite


# ---------------------------------------------------------------------------
# Price improvement scoring
# ---------------------------------------------------------------------------

class TestPriceImprovementScoring:
    """Tests for price improvement scoring."""

    def test_buy_best_ask_gets_score_1(self):
        """For buys, the venue with the best (lowest) ask gets price_score=1.0."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {"randomize_pct": 0, "score_noise": 0})
        venue_data = {
            "BINANCE": {"bid": 99, "ask": 100, "bid_size": 10, "ask_size": 10},
            "KRAKEN":  {"bid": 99, "ask": 105, "bid_size": 10, "ask_size": 10},
        }
        scores = sor._score_venues(venue_data, "BTC/USD", Side.Buy)
        binance = next(s for s in scores if s.exchange == "BINANCE")
        assert binance.price_score == pytest.approx(1.0)

    def test_sell_best_bid_gets_score_1(self):
        """For sells, the venue with the best (highest) bid gets price_score=1.0."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {"randomize_pct": 0, "score_noise": 0})
        venue_data = {
            "BINANCE": {"bid": 105, "ask": 106, "bid_size": 10, "ask_size": 10},
            "KRAKEN":  {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 10},
        }
        scores = sor._score_venues(venue_data, "BTC/USD", Side.Sell)
        binance = next(s for s in scores if s.exchange == "BINANCE")
        assert binance.price_score == pytest.approx(1.0)

    def test_worse_price_gets_lower_score(self):
        """The venue with a worse ask should get a price_score < 1.0."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {"randomize_pct": 0, "score_noise": 0})
        venue_data = {
            "BINANCE": {"bid": 99, "ask": 100, "bid_size": 10, "ask_size": 10},
            "KRAKEN":  {"bid": 99, "ask": 110, "bid_size": 10, "ask_size": 10},
        }
        scores = sor._score_venues(venue_data, "BTC/USD", Side.Buy)
        kraken = next(s for s in scores if s.exchange == "KRAKEN")
        assert kraken.price_score == pytest.approx(100.0 / 110.0)


# ---------------------------------------------------------------------------
# Rejection handling
# ---------------------------------------------------------------------------

class TestRejectionHandling:
    """Tests for child order rejection handling."""

    @pytest.mark.asyncio
    async def test_reject_reroutes_to_other_venue(self):
        """When a venue rejects, the SOR re-routes remaining qty excluding it."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {
            "routing_mode": "best",
            "randomize_pct": 0,
            "score_noise": 0,
        })
        parent = _make_parent(qty=1.0)
        sor._venue_market_data = {
            "BINANCE": {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 10},
            "KRAKEN":  {"bid": 100, "ask": 102, "bid_size": 10, "ask_size": 10},
        }
        await sor.start(parent)
        assert engine.send_child_order.call_count == 1

        # Get the child that was sent to BINANCE (best venue)
        child = list(parent.child_orders.values())[0]
        parent.process_reject(child.cl_ord_id, "Rate limited")

        # Reset call count to track re-route
        engine.send_child_order.reset_mock()

        await sor.on_reject(child, "Rate limited")
        # Should have re-routed to a different venue
        assert engine.send_child_order.call_count >= 1

    @pytest.mark.asyncio
    async def test_reject_does_nothing_when_complete(self):
        """No re-routing if the parent is already fully filled."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {"routing_mode": "best", "randomize_pct": 0, "score_noise": 0})
        parent = _make_parent(qty=1.0)
        sor._venue_market_data = {
            "BINANCE": {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 10},
        }
        await sor.start(parent)

        # Simulate full fill
        child = list(parent.child_orders.values())[0]
        parent.process_fill(child.cl_ord_id, 1.0, 101.0)
        await sor.on_fill(child, 1.0, 101.0)

        engine.send_child_order.reset_mock()
        # Create a synthetic rejected child
        rejected = ChildOrder("R-1", "P-001", "BTC/USD", Side.Buy, 0.5, 101, OrdType.Limit, "KRAKEN")
        rejected.status = "REJECTED"
        await sor.on_reject(rejected, "test")
        assert engine.send_child_order.call_count == 0


# ---------------------------------------------------------------------------
# Anti-gaming randomization
# ---------------------------------------------------------------------------

class TestAntiGaming:
    """Tests for anti-gaming randomization."""

    def test_randomize_qty_changes_value(self):
        """Randomization should produce a value different from the input (statistically)."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {"randomize_pct": 0.10})
        random.seed(42)
        results = [sor._randomize_qty(100.0) for _ in range(100)]
        # At least some values should differ from 100.0
        assert any(abs(r - 100.0) > 0.01 for r in results)

    def test_randomize_qty_within_bounds(self):
        """Randomized qty should be within +/- randomize_pct of original."""
        engine = _make_engine()
        pct = 0.05
        sor = SmartOrderRouter(engine, {"randomize_pct": pct})
        random.seed(42)
        base = 100.0
        for _ in range(1000):
            r = sor._randomize_qty(base)
            assert r >= base * (1 - pct)
            assert r <= base * (1 + pct)

    def test_randomize_zero_pct_unchanged(self):
        """With randomize_pct=0, quantity is unchanged."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {"randomize_pct": 0})
        assert sor._randomize_qty(42.0) == 42.0

    def test_score_noise_adds_variability(self):
        """Score noise should cause composite scores to vary between runs."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {"randomize_pct": 0, "score_noise": 0.1})
        venue_data = {
            "BINANCE": {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 10},
            "KRAKEN":  {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 10},
        }
        composites = set()
        for seed in range(20):
            random.seed(seed)
            scores = sor._score_venues(venue_data, "BTC/USD", Side.Buy)
            composites.add(round(scores[0].composite, 6))
        # With noise, we should see more than 1 unique composite for the top scorer
        assert len(composites) > 1


# ---------------------------------------------------------------------------
# Minimum order size enforcement
# ---------------------------------------------------------------------------

class TestMinOrderSize:
    """Tests for minimum order size enforcement in split mode."""

    def test_min_order_size_skips_small_allocation(self):
        """When a venue's proportional allocation is below min size, it is skipped."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {
            "routing_mode": "split",
            "max_venues": 3,
            "randomize_pct": 0,
            "score_noise": 0,
            "min_order_sizes": {
                "BINANCE": {"BTC/USD": 0.001},
                "KRAKEN":  {"BTC/USD": 0.001},
                "OKX":     {"BTC/USD": 5.0},  # very high min
            },
        })
        sor._venue_market_data = {
            "BINANCE": {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 5},
            "KRAKEN":  {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 4},
            "OKX":     {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 1},
        }
        allocs = sor.route_order("BTC/USD", Side.Buy, 2.0)
        exchanges = {a.exchange for a in allocs}
        # OKX's proportional alloc (2.0 * 1/10 = 0.2) < min 5.0, should be skipped
        assert "OKX" not in exchanges

    def test_min_order_size_default(self):
        """When exchange/symbol not in min_order_sizes, default 0.0001 is used."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {"min_order_sizes": {}})
        assert sor._get_min_order_size("BINANCE", "BTC/USD") == 0.0001


# ---------------------------------------------------------------------------
# Quantity accounting (no leaks)
# ---------------------------------------------------------------------------

class TestQuantityAccounting:
    """Tests that all quantity is accounted for — no leaks."""

    def test_split_total_equals_requested(self):
        """The sum of split allocations must equal the requested quantity."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {
            "routing_mode": "split",
            "max_venues": 5,
            "randomize_pct": 0,
            "score_noise": 0,
        })
        sor._venue_market_data = {
            "BINANCE":  {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 3},
            "KRAKEN":   {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 2},
            "OKX":      {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 5},
        }
        allocs = sor.route_order("BTC/USD", Side.Buy, 10.0)
        total = sum(a.qty for a in allocs)
        assert abs(total - 10.0) < 1e-9

    def test_best_mode_total_equals_requested(self):
        """In best mode with no randomization, allocated qty equals requested."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {
            "routing_mode": "best",
            "randomize_pct": 0,
            "score_noise": 0,
        })
        sor._venue_market_data = {
            "BINANCE": {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 10},
        }
        allocs = sor.route_order("BTC/USD", Side.Buy, 7.77)
        assert allocs[0].qty == pytest.approx(7.77)


# ---------------------------------------------------------------------------
# Fallback routing
# ---------------------------------------------------------------------------

class TestFallbackRouting:
    """Tests for fallback routing when no market data is available."""

    def test_no_market_data_uses_default_routing(self):
        """With no market data, fallback to DEFAULT_ROUTING for the symbol."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {"randomize_pct": 0, "score_noise": 0})
        # No venue market data set
        allocs = sor.route_order("BTC/USD", Side.Buy, 5.0)
        assert len(allocs) == 1
        assert allocs[0].exchange == "BINANCE"  # BTC/USD defaults to BINANCE
        assert allocs[0].qty == 5.0

    def test_fallback_for_eth(self):
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {"randomize_pct": 0, "score_noise": 0})
        allocs = sor.route_order("ETH/USD", Side.Sell, 10.0)
        assert allocs[0].exchange == "KRAKEN"  # ETH/USD defaults to KRAKEN


# ---------------------------------------------------------------------------
# on_tick handler
# ---------------------------------------------------------------------------

class TestOnTick:
    """Tests for on_tick market data processing."""

    @pytest.mark.asyncio
    async def test_on_tick_updates_venue_cache(self):
        """on_tick should populate the venue market data cache."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine)
        parent = _make_parent()
        await sor.start(parent)
        # Clear any child orders from on_start (fallback route)
        engine.send_child_order.reset_mock()

        tick = _venue_tick(bid=100, ask=101, bid_size=5, ask_size=5, exchange="BINANCE")
        await sor.on_tick("BTC/USD", tick)

        assert "BINANCE" in sor._venue_market_data
        assert sor._venue_market_data["BINANCE"]["bid"] == 100

    @pytest.mark.asyncio
    async def test_on_tick_ignores_other_symbols(self):
        """on_tick ignores ticks for symbols other than the parent order's."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine)
        parent = _make_parent(symbol="BTC/USD")
        await sor.start(parent)

        tick = _venue_tick(bid=100, ask=101, exchange="BINANCE")
        await sor.on_tick("ETH/USD", tick)

        assert "BINANCE" not in sor._venue_market_data

    @pytest.mark.asyncio
    async def test_on_tick_ignores_when_paused(self):
        """on_tick does nothing when strategy is paused."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine)
        parent = _make_parent()
        await sor.start(parent)
        await sor.pause()

        tick = _venue_tick(bid=100, ask=101, exchange="BINANCE")
        await sor.on_tick("BTC/USD", tick)

        assert "BINANCE" not in sor._venue_market_data


# ---------------------------------------------------------------------------
# Lifecycle and completion
# ---------------------------------------------------------------------------

class TestLifecycle:
    """Tests for strategy lifecycle (start, fill, completion)."""

    @pytest.mark.asyncio
    async def test_start_submits_child_orders(self):
        """on_start should submit at least one child order."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {
            "routing_mode": "best",
            "randomize_pct": 0,
            "score_noise": 0,
        })
        parent = _make_parent(qty=1.0)
        sor._venue_market_data = {
            "BINANCE": {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 10},
        }
        await sor.start(parent)
        assert engine.send_child_order.call_count == 1
        assert parent.state == ParentState.ACTIVE

    @pytest.mark.asyncio
    async def test_full_fill_completes_parent(self):
        """Parent moves to DONE when child is fully filled."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {
            "routing_mode": "best",
            "randomize_pct": 0,
            "score_noise": 0,
        })
        parent = _make_parent(qty=1.0)
        sor._venue_market_data = {
            "BINANCE": {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 10},
        }
        await sor.start(parent)

        child = list(parent.child_orders.values())[0]
        parent.process_fill(child.cl_ord_id, 1.0, 101.0)
        await sor.on_fill(child, 1.0, 101.0)

        assert parent.state == ParentState.DONE
        assert not sor._active

    @pytest.mark.asyncio
    async def test_stop_cancels_children(self):
        """Stopping the strategy should cancel all outstanding children."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {"routing_mode": "best", "randomize_pct": 0, "score_noise": 0})
        parent = _make_parent(qty=1.0)
        sor._venue_market_data = {
            "BINANCE": {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 10},
        }
        await sor.start(parent)
        await sor.stop()

        assert not sor._active
        assert parent.state == ParentState.CANCELLED
        assert engine.cancel_child_order.call_count >= 1


# ---------------------------------------------------------------------------
# Parameter handling
# ---------------------------------------------------------------------------

class TestParameterHandling:
    """Tests for parameter parsing and defaults."""

    def test_default_parameters(self):
        """Default parameters should be set correctly."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine)
        assert sor.routing_mode == "best"
        assert sor.max_venues == 3
        assert sor.urgency == 0.0
        assert sor.w_price == pytest.approx(0.4)
        assert sor.w_size == pytest.approx(0.3)
        assert sor.w_fee == pytest.approx(0.2)
        assert sor.w_latency == pytest.approx(0.1)
        assert sor.randomize_pct == pytest.approx(0.02)
        assert sor.score_noise == pytest.approx(0.01)

    def test_custom_weights_via_score_weights(self):
        """score_weights dict should override individual weight defaults."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {
            "score_weights": {"w_price": 0.7, "w_size": 0.1, "w_fee": 0.1, "w_latency": 0.1}
        })
        assert sor.w_price == pytest.approx(0.7)
        assert sor.w_size == pytest.approx(0.1)

    def test_custom_routing_mode(self):
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {"routing_mode": "spray"})
        assert sor.routing_mode == "spray"

    def test_custom_fee_tiers(self):
        engine = _make_engine()
        custom_fees = {"BINANCE": {"taker": 0.0005}}
        sor = SmartOrderRouter(engine, {"fee_tiers": custom_fees})
        assert sor.fee_schedule["BINANCE"]["taker"] == 0.0005

    def test_custom_latency_estimates(self):
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {"latency_estimates": {"BINANCE": 0.05}})
        assert sor.latency_estimates["BINANCE"] == 0.05


# ---------------------------------------------------------------------------
# Exclude venues
# ---------------------------------------------------------------------------

class TestExcludeVenues:
    """Tests for excluding venues from routing."""

    def test_exclude_removes_venue(self):
        """Excluded venues should not appear in allocations."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {"routing_mode": "best", "randomize_pct": 0, "score_noise": 0})
        sor._venue_market_data = {
            "BINANCE": {"bid": 100, "ask": 99,  "bid_size": 10, "ask_size": 10},
            "KRAKEN":  {"bid": 100, "ask": 102, "bid_size": 10, "ask_size": 10},
        }
        allocs = sor.route_order("BTC/USD", Side.Buy, 1.0, exclude_venues={"BINANCE"})
        assert all(a.exchange != "BINANCE" for a in allocs)


# ---------------------------------------------------------------------------
# Latency update from fills
# ---------------------------------------------------------------------------

class TestLatencyUpdate:
    """Tests for latency tracking via fill events."""

    @pytest.mark.asyncio
    async def test_fill_updates_latency_estimate(self):
        """Fill events should update the latency estimate for the exchange."""
        engine = _make_engine()
        sor = SmartOrderRouter(engine, {"routing_mode": "best", "randomize_pct": 0, "score_noise": 0})
        parent = _make_parent(qty=1.0)
        sor._venue_market_data = {
            "BINANCE": {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 10},
        }
        await sor.start(parent)

        child = list(parent.child_orders.values())[0]
        # Record a known send time
        sor._child_send_times[child.cl_ord_id] = time.time() - 0.05  # 50ms ago

        old_est = sor.latency_estimates.get("BINANCE", 0.1)
        parent.process_fill(child.cl_ord_id, 1.0, 101.0)
        await sor.on_fill(child, 1.0, 101.0)

        # Latency estimate should have been updated (EMA)
        new_est = sor.latency_estimates.get("BINANCE", old_est)
        assert new_est != old_est


# ---------------------------------------------------------------------------
# Engine market data fallback
# ---------------------------------------------------------------------------

class TestEngineMarketDataFallback:
    """Tests for falling back to engine._market_data when local cache is empty."""

    def test_uses_engine_market_data(self):
        """When local cache is empty, _get_venue_data reads from engine._market_data."""
        engine = _make_engine({
            "BTC/USD:BINANCE": {"bid": 100, "ask": 101, "bid_size": 10, "ask_size": 10},
        })
        sor = SmartOrderRouter(engine, {"randomize_pct": 0, "score_noise": 0})
        # Local cache is empty
        venue_data = sor._get_venue_data("BTC/USD", set())
        assert "BINANCE" in venue_data

    def test_local_cache_takes_priority(self):
        """When local cache has data, engine._market_data is not used."""
        engine = _make_engine({
            "BTC/USD:BINANCE": {"bid": 90, "ask": 91, "bid_size": 10, "ask_size": 10},
        })
        sor = SmartOrderRouter(engine, {"randomize_pct": 0, "score_noise": 0})
        sor._venue_market_data = {
            "BINANCE": {"bid": 100, "ask": 101, "bid_size": 5, "ask_size": 5},
        }
        venue_data = sor._get_venue_data("BTC/USD", set())
        assert venue_data["BINANCE"]["bid"] == 100  # local cache value
