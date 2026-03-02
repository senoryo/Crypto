"""Tests for algo/strategies/sor.py — Smart Order Router."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from algo.strategies.sor import (
    SmartOrderRouter,
    RouteAllocation,
    VenueScore,
    DEFAULT_FEE_SCHEDULE,
    DEFAULT_LATENCY_ESTIMATES,
)
from algo.parent_order import ParentOrder, ChildOrder, ParentState
from algo.engine import AlgoEngine
from shared.fix_protocol import Side, OrdType


# --- Helpers ---

def _mock_engine(market_data: dict | None = None) -> MagicMock:
    """Create a mock engine with configurable market data."""
    engine = MagicMock(spec=AlgoEngine)
    engine.send_child_order = AsyncMock(return_value=True)
    engine.cancel_child_order = AsyncMock(return_value=True)
    engine._market_data = market_data or {}
    return engine


def _make_sor(engine=None, params=None) -> SmartOrderRouter:
    if engine is None:
        engine = _mock_engine()
    sor = SmartOrderRouter(engine, params)
    return sor


def _make_parent(parent_id="ALGO-SOR-1", **kwargs) -> ParentOrder:
    defaults = dict(
        parent_id=parent_id,
        symbol="BTC/USD",
        side=Side.Buy,
        total_qty=10.0,
        algo_type="SOR",
        arrival_price=50000.0,
    )
    defaults.update(kwargs)
    return ParentOrder(**defaults)


# --- Venue scoring ---

class TestVenueScoring:

    def test_single_venue_perfect_score(self):
        market_data = {
            "BTC/USD": {"bid": 50000, "ask": 50100, "bid_size": 10, "ask_size": 10},
        }
        engine = _mock_engine(market_data)
        sor = _make_sor(engine, params={
            "fee_schedule": {"BINANCE": {"maker": 0.001, "taker": 0.001}},
            "score_noise": 0,  # Disable noise for predictable tests
        })

        scores = sor._score_venues(
            {"BINANCE": market_data["BTC/USD"]},
            "BTC/USD",
            Side.Buy,
        )
        assert len(scores) == 1
        # With only one venue, all normalized scores should be 1.0
        assert scores[0].price_score == pytest.approx(1.0)
        assert scores[0].size_score == pytest.approx(1.0)
        assert scores[0].fee_score == pytest.approx(1.0)

    def test_two_venues_price_ranking(self):
        """The venue with the lower ask should rank higher for a buy."""
        venue_data = {
            "BINANCE": {"bid": 50000, "ask": 50050, "bid_size": 10, "ask_size": 10},
            "COINBASE": {"bid": 49990, "ask": 50100, "bid_size": 10, "ask_size": 10},
        }
        engine = _mock_engine()
        sor = _make_sor(engine, params={
            "fee_schedule": {
                "BINANCE": {"maker": 0.001, "taker": 0.001},
                "COINBASE": {"maker": 0.001, "taker": 0.001},
            },
            "score_noise": 0,
        })

        scores = sor._score_venues(venue_data, "BTC/USD", Side.Buy)
        binance = next(s for s in scores if s.exchange == "BINANCE")
        coinbase = next(s for s in scores if s.exchange == "COINBASE")
        assert binance.price_score > coinbase.price_score

    def test_two_venues_fee_ranking(self):
        """Lower-fee venue should score higher on fee dimension."""
        venue_data = {
            "OKX": {"bid": 50000, "ask": 50100, "bid_size": 10, "ask_size": 10},
            "COINBASE": {"bid": 50000, "ask": 50100, "bid_size": 10, "ask_size": 10},
        }
        engine = _mock_engine()
        sor = _make_sor(engine, params={
            "fee_schedule": {
                "OKX": {"maker": 0.0008, "taker": 0.001},
                "COINBASE": {"maker": 0.004, "taker": 0.006},
            },
            "score_noise": 0,
        })

        scores = sor._score_venues(venue_data, "BTC/USD", Side.Buy)
        okx = next(s for s in scores if s.exchange == "OKX")
        coinbase = next(s for s in scores if s.exchange == "COINBASE")
        assert okx.fee_score > coinbase.fee_score

    def test_sell_side_scoring(self):
        """For sells, higher bid should rank better."""
        venue_data = {
            "BINANCE": {"bid": 50100, "ask": 50200, "bid_size": 10, "ask_size": 10},
            "COINBASE": {"bid": 50000, "ask": 50200, "bid_size": 10, "ask_size": 10},
        }
        engine = _mock_engine()
        sor = _make_sor(engine, params={
            "fee_schedule": {
                "BINANCE": {"maker": 0.001, "taker": 0.001},
                "COINBASE": {"maker": 0.001, "taker": 0.001},
            },
            "score_noise": 0,
        })

        scores = sor._score_venues(venue_data, "BTC/USD", Side.Sell)
        binance = next(s for s in scores if s.exchange == "BINANCE")
        coinbase = next(s for s in scores if s.exchange == "COINBASE")
        assert binance.price_score > coinbase.price_score


# --- Route allocation ---

class TestRouteAllocation:

    def test_best_mode_routes_to_single_venue(self):
        """In 'best' mode (default), routes everything to the best venue."""
        market_data = {
            "BTC/USD": {"bid": 50000, "ask": 50100, "bid_size": 100, "ask_size": 100},
        }
        engine = _mock_engine(market_data)
        sor = _make_sor(engine, params={
            "fee_schedule": {"BINANCE": {"maker": 0.001, "taker": 0.001}},
            "randomize_pct": 0,
            "score_noise": 0,
        })

        allocations = sor.route_order("BTC/USD", Side.Buy, 5.0)
        assert len(allocations) == 1
        assert allocations[0].exchange == "BINANCE"
        assert allocations[0].qty == 5.0

    def test_split_mode_splits_across_venues(self):
        """In 'split' mode, when best venue lacks liquidity, split across venues."""
        engine = _mock_engine({})
        sor = _make_sor(engine, params={
            "fee_schedule": {
                "BINANCE": {"maker": 0.001, "taker": 0.001},
                "COINBASE": {"maker": 0.001, "taker": 0.001},
            },
            "randomize_pct": 0,
            "score_noise": 0,
            "routing_mode": "split",
        })
        # Inject venue data directly into SOR's local cache
        sor._venue_market_data = {
            "BINANCE": {"bid": 50000, "ask": 50100, "bid_size": 3, "ask_size": 3},
            "COINBASE": {"bid": 49990, "ask": 50100, "bid_size": 7, "ask_size": 7},
        }

        allocations = sor.route_order("BTC/USD", Side.Buy, 10.0)
        assert len(allocations) >= 2
        total_qty = sum(a.qty for a in allocations)
        assert total_qty == pytest.approx(10.0, rel=0.01)

    def test_exclude_venues(self):
        """Excluded venues should not appear in allocations."""
        market_data = {
            "BTC/USD": {"bid": 50000, "ask": 50100, "bid_size": 100, "ask_size": 100},
        }
        engine = _mock_engine(market_data)
        sor = _make_sor(engine, params={
            "fee_schedule": {
                "BINANCE": {"maker": 0.001, "taker": 0.001},
                "COINBASE": {"maker": 0.001, "taker": 0.001},
            },
            "randomize_pct": 0,
            "score_noise": 0,
        })

        allocations = sor.route_order(
            "BTC/USD", Side.Buy, 5.0, exclude_venues={"BINANCE"}
        )
        for alloc in allocations:
            assert alloc.exchange != "BINANCE"

    def test_fallback_when_no_market_data(self):
        """With no market data, falls back to default routing."""
        engine = _mock_engine({})
        sor = _make_sor(engine, params={"randomize_pct": 0, "score_noise": 0})

        allocations = sor.route_order("BTC/USD", Side.Buy, 5.0)
        assert len(allocations) == 1
        assert allocations[0].qty == 5.0
        # Default route for BTC/USD is BINANCE
        assert allocations[0].exchange == "BINANCE"


# --- Anti-gaming randomization ---

class TestAntiGaming:

    def test_randomize_qty_within_bounds(self):
        sor = _make_sor(params={"randomize_pct": 0.05})
        for _ in range(100):
            rq = sor._randomize_qty(10.0)
            assert 9.5 <= rq <= 10.5

    def test_randomize_disabled(self):
        sor = _make_sor(params={"randomize_pct": 0})
        assert sor._randomize_qty(10.0) == 10.0

    def test_randomize_produces_variation(self):
        """Over many samples, randomization should produce different values."""
        sor = _make_sor(params={"randomize_pct": 0.05})
        values = {sor._randomize_qty(10.0) for _ in range(50)}
        assert len(values) > 1


# --- Fee schedule ---

class TestFeeSchedule:

    def test_default_fee_schedule_keys(self):
        expected = {"BINANCE", "COINBASE", "KRAKEN", "BYBIT", "OKX", "BITFINEX", "HTX"}
        assert set(DEFAULT_FEE_SCHEDULE.keys()) == expected

    def test_each_entry_has_maker_taker(self):
        for exchange, fees in DEFAULT_FEE_SCHEDULE.items():
            assert "maker" in fees, f"{exchange} missing maker"
            assert "taker" in fees, f"{exchange} missing taker"
            assert fees["maker"] > 0
            assert fees["taker"] > 0


# --- Latency estimates ---

class TestLatencyEstimates:

    def test_default_latency_estimates_keys(self):
        expected = {"BINANCE", "COINBASE", "KRAKEN", "BYBIT", "OKX", "BITFINEX", "HTX"}
        assert set(DEFAULT_LATENCY_ESTIMATES.keys()) == expected

    def test_latency_values_positive(self):
        for exchange, lat in DEFAULT_LATENCY_ESTIMATES.items():
            assert lat > 0, f"{exchange} latency should be positive"


# --- Standalone strategy lifecycle ---

class TestSORAsStrategy:

    @pytest.mark.asyncio
    async def test_start_submits_child_orders(self):
        market_data = {
            "BTC/USD": {"bid": 50000, "ask": 50100, "bid_size": 100, "ask_size": 100},
        }
        engine = _mock_engine(market_data)
        sor = _make_sor(engine, params={
            "fee_schedule": {"BINANCE": {"maker": 0.001, "taker": 0.001}},
            "randomize_pct": 0,
            "score_noise": 0,
        })
        parent = _make_parent()

        await sor.start(parent)

        assert sor._active
        assert parent.state == ParentState.ACTIVE
        assert len(parent.child_orders) >= 1
        assert engine.send_child_order.call_count >= 1

    @pytest.mark.asyncio
    async def test_stop_cancels_strategy(self):
        engine = _mock_engine({
            "BTC/USD": {"bid": 50000, "ask": 50100, "bid_size": 100, "ask_size": 100},
        })
        sor = _make_sor(engine, params={
            "fee_schedule": {"BINANCE": {"maker": 0.001, "taker": 0.001}},
            "score_noise": 0,
        })
        parent = _make_parent()
        await sor.start(parent)
        await sor.stop()

        assert not sor._active
        assert parent.state == ParentState.CANCELLED

    @pytest.mark.asyncio
    async def test_fill_completes_parent(self):
        engine = _mock_engine({
            "BTC/USD": {"bid": 50000, "ask": 50100, "bid_size": 100, "ask_size": 100},
        })
        sor = _make_sor(engine, params={
            "fee_schedule": {"BINANCE": {"maker": 0.001, "taker": 0.001}},
            "randomize_pct": 0,
            "score_noise": 0,
        })
        parent = _make_parent(total_qty=1.0)
        await sor.start(parent)

        # Find the child and simulate a full fill
        child_id = list(parent.child_orders.keys())[0]
        child = parent.child_orders[child_id]
        parent.process_fill(child_id, child.qty, 50050.0)

        await sor.on_fill(child, child.qty, 50050.0)
        assert parent.state == ParentState.DONE

    @pytest.mark.asyncio
    async def test_pause_prevents_submissions(self):
        engine = _mock_engine({
            "BTC/USD": {"bid": 50000, "ask": 50100, "bid_size": 100, "ask_size": 100},
        })
        sor = _make_sor(engine, params={
            "fee_schedule": {"BINANCE": {"maker": 0.001, "taker": 0.001}},
            "score_noise": 0,
        })
        parent = _make_parent()
        await sor.start(parent)
        await sor.pause()

        result = await sor.submit_child_order(qty=1.0, price=50000.0)
        assert result is None


# --- Routing modes ---

class TestRoutingModes:

    def test_default_mode_is_best(self):
        sor = _make_sor()
        assert sor.routing_mode == "best"

    def test_split_mode_configurable(self):
        sor = _make_sor(params={"routing_mode": "split"})
        assert sor.routing_mode == "split"

    def test_spray_mode_configurable(self):
        sor = _make_sor(params={"routing_mode": "spray"})
        assert sor.routing_mode == "spray"


# --- Min order size ---

class TestMinOrderSize:

    def test_get_min_order_size(self):
        sor = _make_sor()
        min_size = sor._get_min_order_size("BINANCE", "BTC/USD")
        assert min_size == 0.00001

    def test_get_min_order_size_unknown(self):
        sor = _make_sor()
        min_size = sor._get_min_order_size("UNKNOWN_EXCHANGE", "BTC/USD")
        assert min_size == 0.0001  # default fallback
