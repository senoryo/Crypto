"""
Smart Order Router (SOR) execution strategy.

Routes orders to the best exchange based on a weighted scoring model
considering price, size, fees, and latency. Supports three routing modes:
- best:  Route 100% to the highest-scored venue (default)
- split: Split across top venues proportional to available liquidity
- spray: Spray limit orders across top venues, cancel unfilled on first fill

Can be used as a standalone strategy or as a routing utility by other
strategies (VWAP, TWAP, IS).
"""

import logging
import random
import time
from dataclasses import dataclass, field

from algo.parent_order import ChildOrder, ParentOrder
from algo.strategies.base import BaseStrategy
from shared.fix_protocol import OrdType, Side

logger = logging.getLogger("ALGO")


# --- Fee schedule per exchange (maker/taker in fractional form) ---
DEFAULT_FEE_SCHEDULE: dict[str, dict[str, float]] = {
    "BINANCE":  {"maker": 0.0010, "taker": 0.0010},
    "COINBASE": {"maker": 0.0015, "taker": 0.0015},
    "KRAKEN":   {"maker": 0.0010, "taker": 0.0010},
    "BYBIT":    {"maker": 0.0010, "taker": 0.0010},
    "OKX":      {"maker": 0.0008, "taker": 0.0008},
    "BITFINEX": {"maker": 0.0010, "taker": 0.0010},
    "HTX":      {"maker": 0.0020, "taker": 0.0020},
}

# Default latency estimates per exchange (in seconds)
DEFAULT_LATENCY_ESTIMATES: dict[str, float] = {
    "BINANCE":  0.100,
    "KRAKEN":   0.080,
    "BYBIT":    0.090,
    "OKX":      0.120,
    "BITFINEX": 0.150,
    "HTX":      0.200,
    "COINBASE": 0.130,
}

# Default minimum order sizes per exchange (in base currency units)
DEFAULT_MIN_ORDER_SIZE: dict[str, dict[str, float]] = {
    "BINANCE":  {"BTC/USD": 0.00001, "ETH/USD": 0.0001, "SOL/USD": 0.01, "ADA/USD": 1.0, "DOGE/USD": 1.0},
    "COINBASE": {"BTC/USD": 0.0001,  "ETH/USD": 0.001,  "SOL/USD": 0.01, "ADA/USD": 1.0, "DOGE/USD": 1.0},
    "KRAKEN":   {"BTC/USD": 0.0001,  "ETH/USD": 0.001,  "SOL/USD": 0.01, "ADA/USD": 1.0, "DOGE/USD": 1.0},
    "BYBIT":    {"BTC/USD": 0.00001, "ETH/USD": 0.0001, "SOL/USD": 0.01, "ADA/USD": 1.0, "DOGE/USD": 1.0},
    "OKX":      {"BTC/USD": 0.00001, "ETH/USD": 0.0001, "SOL/USD": 0.01, "ADA/USD": 1.0, "DOGE/USD": 1.0},
    "BITFINEX": {"BTC/USD": 0.0001,  "ETH/USD": 0.001,  "SOL/USD": 0.01, "ADA/USD": 1.0, "DOGE/USD": 1.0},
    "HTX":      {"BTC/USD": 0.0001,  "ETH/USD": 0.001,  "SOL/USD": 0.01, "ADA/USD": 1.0, "DOGE/USD": 1.0},
}


@dataclass
class VenueScore:
    """Composite score for an exchange venue."""
    exchange: str
    price_score: float = 0.0
    size_score: float = 0.0
    fee_score: float = 0.0
    latency_score: float = 0.0
    composite: float = 0.0
    available_qty: float = 0.0
    best_price: float = 0.0


@dataclass
class RouteAllocation:
    """An allocation of quantity to a specific exchange."""
    exchange: str
    qty: float
    price: float = 0.0


class SmartOrderRouter(BaseStrategy):
    """
    Smart Order Router strategy.

    Scores exchanges based on price, liquidity, fees, and latency, then
    routes child orders to the best venue(s). Supports three routing modes:

    - best (default): Route 100% to the highest-scored venue.
    - split: Split across top venues proportional to available liquidity
      when order size exceeds best venue's liquidity.
    - spray: Spray limit orders across top N venues simultaneously;
      cancel unfilled legs when the first fill arrives.

    Also usable as a routing utility by other strategies.
    """

    STRATEGY_NAME = "SOR"

    def __init__(self, engine, params: dict | None = None):
        super().__init__(engine)
        params = params or {}

        # Scoring weights (configurable)
        weights = params.get("score_weights", {})
        self.w_price = weights.get("w_price", params.get("w_price", 0.4))
        self.w_size = weights.get("w_size", params.get("w_size", 0.3))
        self.w_fee = weights.get("w_fee", params.get("w_fee", 0.2))
        self.w_latency = weights.get("w_latency", params.get("w_latency", 0.1))

        # Routing mode: "best", "split", "spray"
        self.routing_mode: str = params.get("routing_mode", "best")

        # Max venues for split/spray
        self.max_venues: int = params.get("max_venues", 3)

        # Urgency (0-1): higher = more aggressive pricing
        self.urgency: float = params.get("urgency", 0.0)

        # Fee schedule (can be overridden)
        self.fee_schedule: dict[str, dict[str, float]] = dict(
            params.get("fee_tiers", params.get("fee_schedule", DEFAULT_FEE_SCHEDULE))
        )

        # Latency estimates per venue (seconds)
        self.latency_estimates: dict[str, float] = dict(
            params.get("latency_estimates", DEFAULT_LATENCY_ESTIMATES)
        )

        # Min order sizes
        self.min_order_sizes: dict[str, dict[str, float]] = dict(
            params.get("min_order_sizes", DEFAULT_MIN_ORDER_SIZE)
        )

        # Anti-gaming: randomization percentage
        self.randomize_pct: float = params.get("randomize_pct", 0.02)

        # Anti-gaming: score noise magnitude
        self.score_noise: float = params.get("score_noise", 0.01)

        # Market data cache: {exchange: {bid, ask, bid_size, ask_size}}
        self._venue_market_data: dict[str, dict] = {}

        # Child order send timestamps for latency measurement
        self._child_send_times: dict[str, float] = {}

        # Spray mode state: tracks active spray legs
        # Maps spray_group_id -> set of child cl_ord_ids in that spray
        self._spray_groups: dict[int, set[str]] = {}
        # Maps child cl_ord_id -> spray_group_id
        self._child_spray_group: dict[str, int] = {}
        self._spray_counter: int = 0
        # Track which spray groups have received their first fill
        self._spray_filled: set[int] = set()

    # --- Standalone strategy lifecycle ---

    async def on_start(self) -> None:
        """Execute the parent order by routing to the best venue(s) immediately."""
        if self.parent_order is None:
            return
        remaining = self.parent_order.remaining_qty()
        if remaining > 0:
            await self._route_order(remaining)

    async def on_tick(self, symbol: str, market_data: dict) -> None:
        """Update venue score cache with latest market data."""
        if not self._active or self._paused:
            return
        if self.parent_order is None:
            return
        if symbol != self.parent_order.symbol:
            return

        # Extract exchange from tick (if present)
        exchange = market_data.get("exchange")
        if exchange:
            bid = market_data.get("bid", market_data.get("price", 0))
            ask = market_data.get("ask", market_data.get("price", 0))
            bid_size = market_data.get("bid_size", market_data.get("volume", 100.0))
            ask_size = market_data.get("ask_size", market_data.get("volume", 100.0))
            if bid > 0 or ask > 0:
                self._venue_market_data[exchange] = {
                    "bid": float(bid),
                    "ask": float(ask),
                    "bid_size": float(bid_size),
                    "ask_size": float(ask_size),
                }

    async def on_fill(self, child_order: ChildOrder, fill_qty: float, fill_price: float) -> None:
        """Track fill latency, handle spray cancellations, check completion."""
        # Record latency
        send_time = self._child_send_times.get(child_order.cl_ord_id)
        if send_time:
            latency = time.time() - send_time
            ex = child_order.exchange
            # Update latency estimates with exponential moving average
            old = self.latency_estimates.get(ex, 1.0)
            self.latency_estimates[ex] = 0.7 * old + 0.3 * latency

        # Spray mode: on first fill in a group, cancel unfilled legs
        spray_group = self._child_spray_group.get(child_order.cl_ord_id)
        if spray_group is not None and spray_group not in self._spray_filled:
            self._spray_filled.add(spray_group)
            siblings = self._spray_groups.get(spray_group, set())
            for sibling_id in siblings:
                if sibling_id != child_order.cl_ord_id:
                    await self.cancel_child_order(sibling_id)

        # Check if parent is fully filled
        if self.parent_order and self.parent_order.is_complete():
            self._active = False
            self.parent_order.complete()
        elif self.parent_order and self._active:
            # If spray child is fully filled but parent isn't, route remaining
            remaining = self.parent_order.remaining_qty()
            if remaining > 0 and child_order.is_terminal:
                # Check if all children in the current batch are terminal
                all_terminal = all(
                    c.is_terminal
                    for c in self.parent_order.child_orders.values()
                )
                if all_terminal:
                    await self._route_order(remaining)

    async def on_cancel_ack(self, child_order: ChildOrder) -> None:
        """Handle spray cancel acknowledgement: return unfilled qty to pool."""
        if self.parent_order is None or not self._active:
            return

        # Check if all children are terminal and there's remaining qty
        remaining = self.parent_order.remaining_qty()
        if remaining <= 0:
            return

        all_terminal = all(
            c.is_terminal
            for c in self.parent_order.child_orders.values()
        )
        if all_terminal:
            await self._route_order(remaining)

    async def on_reject(self, child_order: ChildOrder, reason: str) -> None:
        """On reject, re-route remaining quantity excluding the rejecting exchange."""
        logger.warning(
            "SOR child order %s rejected by %s: %s",
            child_order.cl_ord_id,
            child_order.exchange,
            reason,
        )
        if self.parent_order and not self.parent_order.is_complete() and self._active:
            remaining = self.parent_order.remaining_qty()
            if remaining > 0:
                allocations = self.route_order(
                    self.parent_order.symbol,
                    self.parent_order.side,
                    remaining,
                    exclude_venues={child_order.exchange},
                )
                for alloc in allocations:
                    await self.submit_child_order(
                        qty=alloc.qty,
                        price=alloc.price,
                        ord_type=OrdType.Limit if alloc.price > 0 else OrdType.Market,
                        exchange=alloc.exchange,
                    )

    async def submit_child_order(self, qty, price=0.0, ord_type=OrdType.Limit, exchange=""):
        """Override to track send time for latency measurement."""
        cl_ord_id = await super().submit_child_order(qty, price, ord_type, exchange)
        if cl_ord_id:
            self._child_send_times[cl_ord_id] = time.time()
        return cl_ord_id

    # --- Internal routing dispatch ---

    async def _route_order(self, qty: float) -> None:
        """Route qty using the configured routing mode."""
        if self.parent_order is None:
            return

        if self.routing_mode == "spray":
            await self._spray_route(qty)
        else:
            allocations = self.route_order(
                self.parent_order.symbol,
                self.parent_order.side,
                qty,
            )
            for alloc in allocations:
                await self.submit_child_order(
                    qty=alloc.qty,
                    price=alloc.price,
                    ord_type=OrdType.Limit if alloc.price > 0 else OrdType.Market,
                    exchange=alloc.exchange,
                )

    async def _spray_route(self, qty: float) -> None:
        """Spray limit orders across top N venues simultaneously."""
        if self.parent_order is None:
            return

        venue_data = self._get_venue_data(self.parent_order.symbol, set())
        if not venue_data:
            allocs = self._fallback_route(self.parent_order.symbol, qty)
            for alloc in allocs:
                await self.submit_child_order(
                    qty=alloc.qty,
                    price=alloc.price,
                    ord_type=OrdType.Limit if alloc.price > 0 else OrdType.Market,
                    exchange=alloc.exchange,
                )
            return

        scores = self._score_venues(venue_data, self.parent_order.symbol, self.parent_order.side)
        if not scores:
            allocs = self._fallback_route(self.parent_order.symbol, qty)
            for alloc in allocs:
                await self.submit_child_order(
                    qty=alloc.qty,
                    price=alloc.price,
                    ord_type=OrdType.Limit if alloc.price > 0 else OrdType.Market,
                    exchange=alloc.exchange,
                )
            return

        # Take top N venues
        top_venues = scores[: self.max_venues]

        # Create a spray group
        self._spray_counter += 1
        group_id = self._spray_counter
        self._spray_groups[group_id] = set()

        # Send the same quantity to each venue (spray pattern)
        for vs in top_venues:
            spray_qty = self._randomize_qty(qty)
            cl_ord_id = await self.submit_child_order(
                qty=spray_qty,
                price=vs.best_price,
                ord_type=OrdType.Limit,
                exchange=vs.exchange,
            )
            if cl_ord_id:
                self._spray_groups[group_id].add(cl_ord_id)
                self._child_spray_group[cl_ord_id] = group_id

    # --- Routing logic (usable by other strategies as a utility) ---

    def route_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        exclude_venues: set[str] | None = None,
    ) -> list[RouteAllocation]:
        """
        Determine how to route an order across exchanges.

        Args:
            symbol: Trading symbol (e.g., "BTC/USD").
            side: Side.Buy or Side.Sell.
            qty: Quantity to route.
            exclude_venues: Exchanges to skip (e.g., after rejection).

        Returns:
            List of RouteAllocations specifying exchange and quantity.
        """
        exclude_venues = exclude_venues or set()

        # Get market data per exchange
        venue_data = self._get_venue_data(symbol, exclude_venues)
        if not venue_data:
            return self._fallback_route(symbol, qty)

        # Score each venue
        scores = self._score_venues(venue_data, symbol, side)
        if not scores:
            return self._fallback_route(symbol, qty)

        # Allocate quantity
        return self._allocate(scores, qty, symbol)

    def _get_venue_data(
        self, symbol: str, exclude_venues: set[str]
    ) -> dict[str, dict]:
        """
        Collect market data for each exchange.

        Uses the local cache first (populated via on_tick), then falls back
        to the engine's market data cache.

        Returns {exchange: {bid, ask, bid_size, ask_size}}.
        """
        result = {}

        # First, use local cache from on_tick
        for exchange, data in self._venue_market_data.items():
            if exchange not in exclude_venues:
                result[exchange] = data

        # Fall back to engine market data if local cache is empty
        if not result:
            market_data = getattr(self.engine, "_market_data", {})
            for exchange in self.fee_schedule:
                if exchange in exclude_venues:
                    continue
                key = f"{symbol}:{exchange}"
                tick = market_data.get(key) or market_data.get(symbol)
                if tick:
                    bid = tick.get("bid", tick.get("price", 0))
                    ask = tick.get("ask", tick.get("price", 0))
                    bid_size = tick.get("bid_size", tick.get("volume", 100.0))
                    ask_size = tick.get("ask_size", tick.get("volume", 100.0))
                    if bid > 0 or ask > 0:
                        result[exchange] = {
                            "bid": float(bid),
                            "ask": float(ask),
                            "bid_size": float(bid_size),
                            "ask_size": float(ask_size),
                        }

        return result

    def _score_venues(
        self,
        venue_data: dict[str, dict],
        symbol: str,
        side: str,
    ) -> list[VenueScore]:
        """Score each venue using the weighted composite model."""
        if not venue_data:
            return []

        # Find best price across all venues
        if side == Side.Buy:
            asks = [d["ask"] for d in venue_data.values() if d["ask"] > 0]
            if not asks:
                return []
            best_price = min(asks)
        else:
            bids = [d["bid"] for d in venue_data.values() if d["bid"] > 0]
            if not bids:
                return []
            best_price = max(bids)

        if best_price <= 0:
            return []

        # Find max available size for normalization
        if side == Side.Buy:
            max_size = max(d["ask_size"] for d in venue_data.values())
        else:
            max_size = max(d["bid_size"] for d in venue_data.values())
        max_size = max(max_size, 1e-9)

        # Fee normalization
        fees = []
        for ex in venue_data:
            fee_info = self.fee_schedule.get(ex, {"taker": 0.001})
            fees.append(fee_info.get("taker", 0.001))
        min_fee = min(fees) if fees else 0.001
        max_fee = max(fees) if fees else 0.001
        fee_range = max(max_fee - min_fee, 1e-9)

        # Latency normalization
        latencies = []
        for ex in venue_data:
            latencies.append(self.latency_estimates.get(ex, 1.0))
        min_latency = min(latencies) if latencies else 1.0
        max_latency = max(latencies) if latencies else 1.0
        latency_range = max(max_latency - min_latency, 1e-9)

        scores = []
        for exchange, data in venue_data.items():
            vs = VenueScore(exchange=exchange)

            # Price score: how close to the best price (1.0 = best)
            if side == Side.Buy:
                venue_price = data["ask"]
                vs.best_price = venue_price
                vs.available_qty = data["ask_size"]
                if venue_price > 0:
                    vs.price_score = best_price / venue_price
                else:
                    vs.price_score = 0.0
            else:
                venue_price = data["bid"]
                vs.best_price = venue_price
                vs.available_qty = data["bid_size"]
                if best_price > 0:
                    vs.price_score = venue_price / best_price
                else:
                    vs.price_score = 0.0

            # Size score: available liquidity relative to the most liquid venue
            vs.size_score = vs.available_qty / max_size

            # Fee score: lower fees = higher score (1.0 = cheapest)
            fee_info = self.fee_schedule.get(exchange, {"taker": 0.001})
            venue_fee = fee_info.get("taker", 0.001)
            vs.fee_score = 1.0 - (venue_fee - min_fee) / fee_range

            # Latency score: lower latency = higher score
            venue_latency = self.latency_estimates.get(exchange, 1.0)
            vs.latency_score = 1.0 - (venue_latency - min_latency) / latency_range

            # Composite weighted score
            vs.composite = (
                self.w_price * vs.price_score
                + self.w_size * vs.size_score
                + self.w_fee * vs.fee_score
                + self.w_latency * vs.latency_score
            )

            # Anti-gaming: add small random noise to scores
            if self.score_noise > 0:
                vs.composite += random.uniform(-self.score_noise, self.score_noise)

            scores.append(vs)

        # Sort descending by composite score
        scores.sort(key=lambda s: s.composite, reverse=True)
        return scores

    def _allocate(
        self,
        scores: list[VenueScore],
        qty: float,
        symbol: str,
    ) -> list[RouteAllocation]:
        """
        Allocate quantity across scored venues.

        In "best" mode: route 100% to the top venue.
        In "split" mode: split proportionally across top venues by liquidity.
        """
        if not scores:
            return []

        best = scores[0]

        if self.routing_mode == "best":
            # Route everything to the best venue
            final_qty = self._randomize_qty(qty)
            return [RouteAllocation(
                exchange=best.exchange,
                qty=final_qty,
                price=best.best_price,
            )]

        # Split mode: check if best venue has enough liquidity
        if best.available_qty >= qty:
            final_qty = self._randomize_qty(qty)
            return [RouteAllocation(
                exchange=best.exchange,
                qty=final_qty,
                price=best.best_price,
            )]

        # Split across top venues proportional to available liquidity
        top = scores[: self.max_venues]
        total_available = sum(s.available_qty for s in top)
        if total_available <= 0:
            final_qty = self._randomize_qty(qty)
            return [RouteAllocation(
                exchange=best.exchange,
                qty=final_qty,
                price=best.best_price,
            )]

        allocations: list[RouteAllocation] = []
        remaining = qty
        for vs in top:
            if remaining <= 0:
                break

            proportion = vs.available_qty / total_available
            alloc_qty = min(qty * proportion, remaining, vs.available_qty)

            min_size = self._get_min_order_size(vs.exchange, symbol)
            if alloc_qty < min_size:
                continue

            alloc_qty = self._randomize_qty(alloc_qty)
            alloc_qty = min(alloc_qty, remaining)

            allocations.append(RouteAllocation(
                exchange=vs.exchange,
                qty=alloc_qty,
                price=vs.best_price,
            ))
            remaining -= alloc_qty

        # Leftover from rounding/min-size filtering goes to best venue
        if remaining > 0 and allocations:
            allocations[0].qty += remaining
        elif remaining > 0 and not allocations:
            allocations.append(RouteAllocation(
                exchange=best.exchange,
                qty=qty,
                price=best.best_price,
            ))

        return allocations

    def _fallback_route(self, symbol: str, qty: float) -> list[RouteAllocation]:
        """Route to default exchange when no market data is available."""
        from shared.config import DEFAULT_ROUTING
        exchange = DEFAULT_ROUTING.get(symbol, "BINANCE")
        return [RouteAllocation(exchange=exchange, qty=qty)]

    def _get_min_order_size(self, exchange: str, symbol: str) -> float:
        """Get minimum order size for an exchange and symbol."""
        sizes = self.min_order_sizes.get(exchange, {})
        return sizes.get(symbol, 0.0001)

    def _randomize_qty(self, qty: float) -> float:
        """
        Apply anti-gaming randomization to order quantity.
        Randomly adjusts by +/- randomize_pct to avoid detection.
        """
        if self.randomize_pct <= 0:
            return qty
        factor = 1.0 + random.uniform(-self.randomize_pct, self.randomize_pct)
        return qty * factor
