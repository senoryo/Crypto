"""
VWAP (Volume-Weighted Average Price) execution strategy.

Executes a parent order by matching the historical intraday volume profile,
minimizing deviation from the day's VWAP. Divides the execution horizon
into time buckets, allocates quantity proportional to the volume profile,
and uses passive-first pricing with aggressive escalation when behind schedule.

Features:
- Configurable volume profile (uniform default)
- Participation rate cap per bucket
- Passive-first pricing with aggressive escalation threshold
- Circuit breakers (price, spread, volume)
- Slippage tracking vs running VWAP
- Residual sweep in final bucket
"""

import asyncio
import logging
import time

from algo.parent_order import ChildOrder, ParentOrder, ParentState
from algo.strategies.base import BaseStrategy
from shared.fix_protocol import OrdType, Side

logger = logging.getLogger("ALGO")

# Circuit breaker defaults
DEFAULT_PRICE_CB_PCT = 0.05       # 5% price move from arrival
DEFAULT_SPREAD_CB_PCT = 0.02      # 2% spread / mid
DEFAULT_VOLUME_CB_MIN = 1e-9      # near-zero volume threshold


class VWAPStrategy(BaseStrategy):
    """
    VWAP execution strategy.

    Divides execution horizon into time buckets weighted by a volume profile.
    Places passive limit orders, escalates to aggressive pricing when behind
    schedule, and sweeps residual quantity in the final bucket.
    """

    STRATEGY_NAME = "VWAP"

    def __init__(self, engine, params=None):
        super().__init__(engine)
        params = params or {}

        # Execution parameters
        self.horizon_seconds: float = params.get("horizon_seconds", 3600)
        self.num_buckets: int = params.get("num_buckets", 12)
        self.participation_cap: float = params.get("participation_cap", 0.15)
        self.price_offset_bps: float = params.get("price_offset_bps", 5)
        self.aggressive_threshold: float = params.get("aggressive_threshold", 0.7)
        self.use_sor: bool = params.get("use_sor", False)

        # Volume profile: list of weights per bucket
        profile = params.get("volume_profile", None)
        if profile is not None:
            self.volume_profile = self._normalize_profile(list(profile))
        else:
            # Uniform distribution
            self.volume_profile = [1.0 / self.num_buckets] * self.num_buckets

        # Ensure num_buckets matches profile length
        self.num_buckets = len(self.volume_profile)

        # Circuit breaker thresholds
        self.price_cb_pct: float = params.get("price_cb_pct", DEFAULT_PRICE_CB_PCT)
        self.spread_cb_pct: float = params.get("spread_cb_pct", DEFAULT_SPREAD_CB_PCT)
        self.volume_cb_min: float = params.get("volume_cb_min", DEFAULT_VOLUME_CB_MIN)

        # Market data state
        self._latest_bid: float = 0.0
        self._latest_ask: float = 0.0
        self._latest_mid: float = 0.0
        self._arrival_price: float = 0.0

        # Running VWAP from market ticks
        self._tick_volume_sum: float = 0.0
        self._tick_pv_sum: float = 0.0  # price * volume sum

        # Per-bucket tracking
        self._bucket_filled: list[float] = []    # qty filled per bucket
        self._bucket_targets: list[float] = []   # target qty per bucket
        self._bucket_market_vol: list[float] = [] # market volume per bucket
        self._current_bucket: int = 0
        self._bucket_start_time: float = 0.0
        self._bucket_duration: float = 0.0

        # Active child orders in current bucket
        self._active_bucket_orders: list[str] = []

        # Scheduler task handle
        self._scheduler_task: asyncio.Task | None = None

        # Circuit breaker state
        self._cb_triggered: bool = False
        self._cb_reason: str = ""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_start(self) -> None:
        """Initialize bucket schedule and start the scheduler."""
        if self.parent_order is None:
            return

        total_qty = self.parent_order.total_qty

        # Calculate per-bucket target quantities
        self._bucket_targets = [
            total_qty * w for w in self.volume_profile
        ]
        self._bucket_filled = [0.0] * self.num_buckets
        self._bucket_market_vol = [0.0] * self.num_buckets
        self._current_bucket = 0

        # Bucket timing
        self._bucket_duration = self.horizon_seconds / self.num_buckets

        # Capture arrival price from current market data
        tick = getattr(self.engine, "_market_data", {}).get(
            self.parent_order.symbol, {}
        )
        bid = tick.get("bid", tick.get("price", 0))
        ask = tick.get("ask", tick.get("price", 0))
        if bid and ask:
            self._arrival_price = (float(bid) + float(ask)) / 2.0
            self._latest_bid = float(bid)
            self._latest_ask = float(ask)
            self._latest_mid = self._arrival_price
        elif bid:
            self._arrival_price = float(bid)
            self._latest_bid = float(bid)
            self._latest_mid = float(bid)
        elif ask:
            self._arrival_price = float(ask)
            self._latest_ask = float(ask)
            self._latest_mid = float(ask)

        # Use parent order arrival price if we couldn't capture one
        if self._arrival_price == 0.0 and self.parent_order.arrival_price > 0:
            self._arrival_price = self.parent_order.arrival_price

        logger.info(
            "VWAP %s starting: %d buckets over %ds, arrival_px=%.6f, total_qty=%.6f",
            self.parent_order.parent_id,
            self.num_buckets,
            self.horizon_seconds,
            self._arrival_price,
            total_qty,
        )

        # Start the scheduler
        self._scheduler_task = asyncio.create_task(self._run_scheduler())

    async def stop(self) -> None:
        """Cancel scheduler and all children."""
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        await super().stop()

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    async def on_tick(self, symbol: str, market_data: dict) -> None:
        """Update market data cache and running VWAP."""
        if not self._active or self._paused:
            return
        if self.parent_order is None:
            return
        if symbol != self.parent_order.symbol:
            return

        bid = float(market_data.get("bid", market_data.get("price", 0)))
        ask = float(market_data.get("ask", market_data.get("price", 0)))
        volume = float(market_data.get("volume", 0))

        if bid > 0:
            self._latest_bid = bid
        if ask > 0:
            self._latest_ask = ask
        if bid > 0 and ask > 0:
            self._latest_mid = (bid + ask) / 2.0
        elif bid > 0:
            self._latest_mid = bid
        elif ask > 0:
            self._latest_mid = ask

        # Update running VWAP from ticks
        if volume > 0 and self._latest_mid > 0:
            self._tick_volume_sum += volume
            self._tick_pv_sum += self._latest_mid * volume

        # Track market volume for current bucket participation rate
        bucket = self._current_bucket
        if 0 <= bucket < self.num_buckets and volume > 0:
            self._bucket_market_vol[bucket] += volume

        # Check circuit breakers
        await self._check_circuit_breakers()

    # ------------------------------------------------------------------
    # Execution reports
    # ------------------------------------------------------------------

    async def on_fill(self, child_order: ChildOrder, fill_qty: float, fill_price: float) -> None:
        """Track fills per bucket, check completion."""
        if self.parent_order is None:
            return

        bucket = self._current_bucket
        if 0 <= bucket < self.num_buckets:
            self._bucket_filled[bucket] += fill_qty

        # Log slippage
        running_vwap = self._calculate_running_vwap()
        if running_vwap > 0 and self.parent_order.avg_fill_price > 0:
            slippage = self.parent_order.avg_fill_price - running_vwap
            sign = 1.0 if self.parent_order.side == Side.Buy else -1.0
            logger.info(
                "VWAP %s fill: qty=%.6f px=%.6f bucket=%d slippage=%.6f",
                self.parent_order.parent_id,
                fill_qty,
                fill_price,
                bucket,
                slippage * sign,
            )

        # Check completion
        if self.parent_order.is_complete():
            self._active = False
            self.parent_order.complete()
            # Cancel scheduler
            if self._scheduler_task and not self._scheduler_task.done():
                self._scheduler_task.cancel()
            logger.info(
                "VWAP %s completed: filled=%.6f avg_px=%.6f",
                self.parent_order.parent_id,
                self.parent_order.filled_qty,
                self.parent_order.avg_fill_price,
            )

    async def on_reject(self, child_order: ChildOrder, reason: str) -> None:
        """Return rejected qty to bucket residual."""
        if self.parent_order is None:
            return

        logger.warning(
            "VWAP %s child %s rejected: %s",
            self.parent_order.parent_id,
            child_order.cl_ord_id,
            reason,
        )
        # The unfilled qty is already tracked by parent_order.remaining_qty(),
        # but we need to know the bucket didn't fill its target
        bucket = self._current_bucket
        if 0 <= bucket < self.num_buckets:
            # Rejected qty doesn't count as filled — nothing to undo
            pass

    async def on_cancel_ack(self, child_order: ChildOrder) -> None:
        """Handle cancel acknowledgement."""
        if self.parent_order is None:
            return
        logger.info(
            "VWAP %s child %s cancelled, leaves_qty=%.6f",
            self.parent_order.parent_id,
            child_order.cl_ord_id,
            child_order.leaves_qty,
        )

    # ------------------------------------------------------------------
    # Scheduler
    # ------------------------------------------------------------------

    async def _run_scheduler(self) -> None:
        """Main bucket scheduling loop."""
        if self.parent_order is None:
            return

        try:
            for bucket_idx in range(self.num_buckets):
                if not self._active:
                    return

                self._current_bucket = bucket_idx
                self._bucket_start_time = time.time()

                # Calculate target for this bucket including any residual
                # from prior buckets
                bucket_target = self._get_bucket_target(bucket_idx)

                is_last_bucket = (bucket_idx == self.num_buckets - 1)

                logger.info(
                    "VWAP %s bucket %d/%d: target=%.6f, remaining=%.6f",
                    self.parent_order.parent_id,
                    bucket_idx + 1,
                    self.num_buckets,
                    bucket_target,
                    self.parent_order.remaining_qty(),
                )

                if bucket_target <= 0 or self.parent_order.is_complete():
                    # Nothing to do in this bucket
                    if not is_last_bucket:
                        await asyncio.sleep(self._bucket_duration)
                    continue

                # Wait while paused or circuit breaker is triggered
                while self._paused or self._cb_triggered:
                    if not self._active:
                        return
                    await asyncio.sleep(0.1)

                # Phase 1: Place passive order
                passive_qty = min(bucket_target, self.parent_order.remaining_qty())
                if passive_qty > 0:
                    await self._place_bucket_order(passive_qty, aggressive=False)

                # Sleep until aggressive threshold
                aggressive_wait = self._bucket_duration * self.aggressive_threshold
                await asyncio.sleep(aggressive_wait)

                if not self._active or self.parent_order.is_complete():
                    return

                # Phase 2: Check progress; escalate if behind
                filled_in_bucket = self._bucket_filled[bucket_idx]
                remaining_target = bucket_target - filled_in_bucket

                if remaining_target > 0:
                    remaining_parent = self.parent_order.remaining_qty()
                    aggressive_qty = min(remaining_target, remaining_parent)

                    if aggressive_qty > 0:
                        logger.info(
                            "VWAP %s bucket %d escalating: unfilled=%.6f, going aggressive",
                            self.parent_order.parent_id,
                            bucket_idx + 1,
                            aggressive_qty,
                        )
                        # Cancel passive orders first
                        await self._cancel_active_bucket_orders()
                        await self._place_bucket_order(aggressive_qty, aggressive=True)

                # Sleep until bucket end
                remaining_time = self._bucket_duration - aggressive_wait
                if remaining_time > 0 and not is_last_bucket:
                    await asyncio.sleep(remaining_time)

                # Cancel any unfilled orders at bucket end
                await self._cancel_active_bucket_orders()

                # Log bucket summary
                self._log_bucket_summary(bucket_idx)

            # After all buckets: sweep residual
            if self._active and self.parent_order and not self.parent_order.is_complete():
                residual = self.parent_order.remaining_qty()
                if residual > 0:
                    if residual > self.parent_order.total_qty * 0.10:
                        logger.warning(
                            "VWAP %s residual %.6f (%.1f%% of total) — "
                            "execution fell significantly behind",
                            self.parent_order.parent_id,
                            residual,
                            (residual / self.parent_order.total_qty) * 100,
                        )
                    logger.info(
                        "VWAP %s sweeping residual %.6f with market order",
                        self.parent_order.parent_id,
                        residual,
                    )
                    await self._place_sweep_order(residual)

        except asyncio.CancelledError:
            logger.info(
                "VWAP %s scheduler cancelled",
                self.parent_order.parent_id if self.parent_order else "?",
            )

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    async def _place_bucket_order(self, qty: float, aggressive: bool = False) -> None:
        """Place a child order for the current bucket."""
        if self.parent_order is None or not self._active:
            return

        # Enforce participation cap
        qty = self._apply_participation_cap(qty)
        if qty <= 0:
            return

        # Determine price
        price = self._compute_price(aggressive)
        if price <= 0:
            # No valid market data — use market order
            cl_ord_id = await self.submit_child_order(
                qty=qty,
                price=0.0,
                ord_type=OrdType.Market,
            )
        else:
            cl_ord_id = await self.submit_child_order(
                qty=qty,
                price=price,
                ord_type=OrdType.Limit,
            )

        if cl_ord_id:
            self._active_bucket_orders.append(cl_ord_id)

    async def _place_sweep_order(self, qty: float) -> None:
        """Place a market order to sweep residual quantity."""
        if self.parent_order is None or not self._active:
            return
        cl_ord_id = await self.submit_child_order(
            qty=qty,
            price=0.0,
            ord_type=OrdType.Market,
        )
        if cl_ord_id:
            self._active_bucket_orders.append(cl_ord_id)

    async def _cancel_active_bucket_orders(self) -> None:
        """Cancel all active orders for the current bucket."""
        for cl_ord_id in list(self._active_bucket_orders):
            await self.cancel_child_order(cl_ord_id)
        self._active_bucket_orders.clear()

    # ------------------------------------------------------------------
    # Pricing
    # ------------------------------------------------------------------

    def _compute_price(self, aggressive: bool) -> float:
        """Compute the order price based on aggression level."""
        if self.parent_order is None:
            return 0.0

        bid = self._latest_bid
        ask = self._latest_ask
        mid = self._latest_mid

        if bid <= 0 and ask <= 0:
            return 0.0

        # Ensure we have both sides
        if bid <= 0:
            bid = ask
        if ask <= 0:
            ask = bid

        offset = self.price_offset_bps / 10000.0  # convert bps to fraction

        if self.parent_order.side == Side.Buy:
            if aggressive:
                # Cross the spread — lift the ask
                return ask
            else:
                # Passive: bid + small offset (inside spread, but passive)
                return bid + (mid * offset)
        else:
            if aggressive:
                # Cross the spread — hit the bid
                return bid
            else:
                # Passive: ask - small offset
                return ask - (mid * offset)

    # ------------------------------------------------------------------
    # Participation cap
    # ------------------------------------------------------------------

    def _apply_participation_cap(self, qty: float) -> float:
        """Reduce qty if it would exceed participation cap of market volume."""
        bucket = self._current_bucket
        if bucket < 0 or bucket >= self.num_buckets:
            return qty

        market_vol = self._bucket_market_vol[bucket]
        if market_vol <= 0:
            # No market volume data yet — allow the order
            return qty

        already_filled = self._bucket_filled[bucket]
        max_allowed = market_vol * self.participation_cap
        room = max(0.0, max_allowed - already_filled)

        if qty > room:
            logger.info(
                "VWAP participation cap: reducing qty from %.6f to %.6f "
                "(market_vol=%.6f, cap=%.1f%%)",
                qty,
                room,
                market_vol,
                self.participation_cap * 100,
            )
            return room

        return qty

    # ------------------------------------------------------------------
    # Bucket helpers
    # ------------------------------------------------------------------

    def _get_bucket_target(self, bucket_idx: int) -> float:
        """Get target qty for a bucket, including residual from prior buckets."""
        if self.parent_order is None:
            return 0.0

        base_target = self._bucket_targets[bucket_idx]

        # Add residual from all prior unfilled buckets
        residual = 0.0
        for i in range(bucket_idx):
            shortfall = self._bucket_targets[i] - self._bucket_filled[i]
            if shortfall > 0:
                residual += shortfall

        target = base_target + residual

        # Don't exceed remaining parent quantity
        return min(target, self.parent_order.remaining_qty())

    def _log_bucket_summary(self, bucket_idx: int) -> None:
        """Log a summary for a completed bucket."""
        if self.parent_order is None:
            return

        target = self._bucket_targets[bucket_idx]
        filled = self._bucket_filled[bucket_idx]
        market_vol = self._bucket_market_vol[bucket_idx]

        participation = (filled / market_vol * 100) if market_vol > 0 else 0.0

        running_vwap = self._calculate_running_vwap()
        avg_fill = self.parent_order.avg_fill_price
        slippage = 0.0
        if running_vwap > 0 and avg_fill > 0:
            sign = 1.0 if self.parent_order.side == Side.Buy else -1.0
            slippage = (avg_fill - running_vwap) * sign

        logger.info(
            "VWAP %s bucket %d summary: target=%.6f filled=%.6f "
            "participation=%.1f%% slippage=%.6f",
            self.parent_order.parent_id,
            bucket_idx + 1,
            target,
            filled,
            participation,
            slippage,
        )

    # ------------------------------------------------------------------
    # Running VWAP
    # ------------------------------------------------------------------

    def _calculate_running_vwap(self) -> float:
        """Calculate running VWAP from accumulated tick data."""
        if self._tick_volume_sum <= 0:
            return 0.0
        return self._tick_pv_sum / self._tick_volume_sum

    # ------------------------------------------------------------------
    # Circuit breakers
    # ------------------------------------------------------------------

    async def _check_circuit_breakers(self) -> None:
        """Check all circuit breakers; pause strategy if any triggers."""
        if not self._active or self.parent_order is None:
            return

        mid = self._latest_mid
        bid = self._latest_bid
        ask = self._latest_ask

        # Price circuit breaker
        if self._arrival_price > 0 and mid > 0:
            price_change = abs(mid - self._arrival_price) / self._arrival_price
            if price_change > self.price_cb_pct:
                if not self._cb_triggered:
                    self._cb_triggered = True
                    self._cb_reason = f"price moved {price_change:.1%} from arrival"
                    logger.warning(
                        "VWAP %s CIRCUIT BREAKER: %s",
                        self.parent_order.parent_id,
                        self._cb_reason,
                    )
                return

        # Spread circuit breaker
        if bid > 0 and ask > 0 and mid > 0:
            spread = (ask - bid) / mid
            if spread > self.spread_cb_pct:
                if not self._cb_triggered:
                    self._cb_triggered = True
                    self._cb_reason = f"spread widened to {spread:.1%}"
                    logger.warning(
                        "VWAP %s CIRCUIT BREAKER: %s",
                        self.parent_order.parent_id,
                        self._cb_reason,
                    )
                return

        # Volume circuit breaker
        bucket = self._current_bucket
        if 0 <= bucket < self.num_buckets:
            elapsed = time.time() - self._bucket_start_time
            if elapsed > self._bucket_duration * 0.3:
                # Only check after 30% of bucket has elapsed
                vol = self._bucket_market_vol[bucket]
                if vol <= self.volume_cb_min:
                    if not self._cb_triggered:
                        self._cb_triggered = True
                        self._cb_reason = "market volume near zero"
                        logger.warning(
                            "VWAP %s CIRCUIT BREAKER: %s",
                            self.parent_order.parent_id,
                            self._cb_reason,
                        )
                    return

        # Reset CB if conditions normalized
        if self._cb_triggered:
            self._cb_triggered = False
            self._cb_reason = ""
            logger.info(
                "VWAP %s circuit breaker reset — conditions normalized",
                self.parent_order.parent_id,
            )

    # ------------------------------------------------------------------
    # Profile normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_profile(profile: list[float]) -> list[float]:
        """Normalize a volume profile so weights sum to 1.0."""
        total = sum(profile)
        if total <= 0:
            n = len(profile)
            return [1.0 / n] * n if n > 0 else []
        return [w / total for w in profile]
