"""
TWAP (Time-Weighted Average Price) execution strategy.

Executes a parent order evenly across a time window, minimizing timing risk.
Divides total quantity into equal slices with jittered timing to avoid
predictable patterns. Each slice starts with a passive limit order and
escalates to aggressive pricing if unfilled within a configurable threshold.
"""

import asyncio
import logging
import math
import random
import time

from algo.parent_order import ChildOrder, ParentOrder
from algo.strategies.base import BaseStrategy
from shared.fix_protocol import OrdType, Side

logger = logging.getLogger("ALGO")


class TWAPStrategy(BaseStrategy):
    """
    TWAP execution strategy.

    Splits total quantity into equal time slices across a horizon, with
    jittered timing and passive-to-aggressive escalation per slice.
    """

    STRATEGY_NAME = "TWAP"

    def __init__(self, engine, params: dict | None = None):
        super().__init__(engine)
        params = params or {}

        # --- Parameters ---
        self.horizon_seconds: float = params.get("horizon_seconds", 3600)
        self.num_slices: int = params.get("num_slices", 20)
        self.jitter_pct: float = params.get("jitter_pct", 0.15)
        self.price_offset_bps: float = params.get("price_offset_bps", 5)
        self.aggressive_threshold: float = params.get("aggressive_threshold", 0.7)
        self.max_slice_retries: int = params.get("max_slice_retries", 2)

        # --- Circuit breaker thresholds ---
        self.price_circuit_pct: float = params.get("price_circuit_pct", 0.05)
        self.spread_circuit_pct: float = params.get("spread_circuit_pct", 0.02)

        # --- Market data state ---
        self._latest_bid: float = 0.0
        self._latest_ask: float = 0.0
        self._latest_mid: float = 0.0
        self._arrival_mid: float = 0.0

        # --- Slice tracking ---
        self._slice_qty: float = 0.0
        self._last_slice_extra: float = 0.0
        self._filled_per_slice: list[float] = []
        self._residual: float = 0.0

        # --- Timing ---
        self._algo_start_time: float = 0.0
        self._algo_end_time: float = 0.0
        self._slice_duration: float = 0.0
        self._jittered_start_times: list[float] = []

        # --- Scheduler ---
        self._scheduler_task: asyncio.Task | None = None
        self._current_slice: int = 0
        self._current_child_id: str | None = None

    # --- Lifecycle ---

    async def on_start(self) -> None:
        """Parse parameters, compute slice schedule, and start the scheduler."""
        if self.parent_order is None:
            return

        total_qty = self.parent_order.total_qty

        # Calculate per-slice quantity
        self._slice_qty = math.floor(total_qty / self.num_slices * 1e8) / 1e8
        allocated = self._slice_qty * self.num_slices
        self._last_slice_extra = total_qty - self._slice_qty * (self.num_slices - 1)
        # If slice_qty rounds down, last slice absorbs the remainder
        if self._slice_qty * self.num_slices < total_qty:
            self._last_slice_extra = total_qty - self._slice_qty * (self.num_slices - 1)

        self._filled_per_slice = [0.0] * self.num_slices
        self._residual = 0.0

        # Timing
        self._algo_start_time = time.time()
        self._algo_end_time = self._algo_start_time + self.horizon_seconds
        self._slice_duration = self.horizon_seconds / self.num_slices

        # Capture arrival mid from current market data
        tick = getattr(self.engine, '_market_data', {}).get(
            self.parent_order.symbol, {}
        )
        bid = tick.get("bid", tick.get("price", 0))
        ask = tick.get("ask", tick.get("price", 0))
        if bid and ask:
            self._arrival_mid = (float(bid) + float(ask)) / 2.0
        elif self.parent_order.arrival_price > 0:
            self._arrival_mid = self.parent_order.arrival_price

        # Generate jittered start times for all slices
        self._jittered_start_times = self._generate_jittered_times()

        logger.info(
            "TWAP %s starting: %s %s qty=%.8f horizon=%ds slices=%d "
            "slice_qty=%.8f jitter=%.1f%% arrival_mid=%.4f",
            self.parent_order.parent_id,
            self.parent_order.symbol,
            self.parent_order.side,
            total_qty,
            self.horizon_seconds,
            self.num_slices,
            self._slice_qty,
            self.jitter_pct * 100,
            self._arrival_mid,
        )

        # Start the async scheduler
        self._scheduler_task = asyncio.ensure_future(self._run_scheduler())

    async def on_tick(self, symbol: str, market_data: dict) -> None:
        """Update latest bid/ask/mid from market data."""
        if not self._active or self._paused:
            return
        if self.parent_order is None:
            return
        if symbol != self.parent_order.symbol:
            return

        bid = market_data.get("bid", market_data.get("price", 0))
        ask = market_data.get("ask", market_data.get("price", 0))

        if bid:
            self._latest_bid = float(bid)
        if ask:
            self._latest_ask = float(ask)
        if self._latest_bid > 0 and self._latest_ask > 0:
            self._latest_mid = (self._latest_bid + self._latest_ask) / 2.0

    async def on_fill(self, child_order: ChildOrder, fill_qty: float, fill_price: float) -> None:
        """Track fills per slice. Check for completion."""
        if self.parent_order is None:
            return

        # Track fill against the current slice
        if 0 <= self._current_slice < self.num_slices:
            self._filled_per_slice[self._current_slice] += fill_qty

        logger.info(
            "TWAP %s fill: slice=%d qty=%.8f px=%.4f total_filled=%.8f/%.8f",
            self.parent_order.parent_id,
            self._current_slice,
            fill_qty,
            fill_price,
            self.parent_order.filled_qty,
            self.parent_order.total_qty,
        )

        # Check if parent is fully filled
        if self.parent_order.is_complete():
            self._active = False
            self.parent_order.complete()
            if self._scheduler_task and not self._scheduler_task.done():
                self._scheduler_task.cancel()
            logger.info(
                "TWAP %s completed: filled=%.8f avg_px=%.6f",
                self.parent_order.parent_id,
                self.parent_order.filled_qty,
                self.parent_order.avg_fill_price,
            )

    async def on_reject(self, child_order: ChildOrder, reason: str) -> None:
        """Add rejected quantity to residual."""
        rejected_qty = child_order.qty - child_order.filled_qty
        self._residual += rejected_qty
        if self._current_child_id == child_order.cl_ord_id:
            self._current_child_id = None

        logger.warning(
            "TWAP %s reject: slice=%d cl=%s reason=%s residual+=%.8f",
            self.parent_order.parent_id if self.parent_order else "?",
            self._current_slice,
            child_order.cl_ord_id,
            reason,
            rejected_qty,
        )

    async def on_cancel_ack(self, child_order: ChildOrder) -> None:
        """Track cancelled quantity."""
        cancelled_qty = child_order.qty - child_order.filled_qty
        if self._current_child_id == child_order.cl_ord_id:
            self._current_child_id = None

        logger.info(
            "TWAP %s cancel_ack: cl=%s unfilled=%.8f",
            self.parent_order.parent_id if self.parent_order else "?",
            child_order.cl_ord_id,
            cancelled_qty,
        )

    async def stop(self) -> None:
        """Cancel the algo and its scheduler task."""
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        await super().stop()

    # --- Scheduler ---

    async def _run_scheduler(self) -> None:
        """
        Main scheduler loop: for each slice, wait for its jittered start,
        place a passive order, escalate if needed, and track residual.
        """
        try:
            for i in range(self.num_slices):
                if not self._active:
                    break
                if self.parent_order and self.parent_order.is_complete():
                    break

                self._current_slice = i

                # --- Wait until this slice's jittered start time ---
                now = time.time()
                target_start = self._jittered_start_times[i]
                wait_time = target_start - now
                if wait_time > 0:
                    await asyncio.sleep(wait_time)

                if not self._active:
                    break
                if self.parent_order and self.parent_order.is_complete():
                    break

                # --- Check circuit breakers ---
                if self._check_circuit_breakers():
                    logger.warning(
                        "TWAP %s circuit breaker triggered at slice %d, pausing",
                        self.parent_order.parent_id if self.parent_order else "?",
                        i,
                    )
                    await self.pause()
                    # Wait until resumed or stopped
                    while self._paused and self._active:
                        await asyncio.sleep(0.1)
                    if not self._active:
                        break

                # --- Determine quantity for this slice ---
                if i == self.num_slices - 1:
                    slice_target = self._last_slice_extra
                else:
                    slice_target = self._slice_qty

                remaining_for_parent = (
                    self.parent_order.remaining_qty() if self.parent_order else 0
                )
                qty = min(slice_target, remaining_for_parent)

                if qty <= 0:
                    continue

                # --- Phase 1: Passive order ---
                child_id = await self._place_slice_order(qty, aggressive=False)
                if child_id is None:
                    self._residual += qty
                    logger.warning(
                        "TWAP %s slice %d: failed to place passive order, "
                        "residual+=%.8f",
                        self.parent_order.parent_id if self.parent_order else "?",
                        i, qty,
                    )
                    continue

                self._current_child_id = child_id

                # --- Phase 2: Wait for aggressive threshold ---
                aggressive_wait = self._slice_duration * self.aggressive_threshold
                await asyncio.sleep(aggressive_wait)

                if not self._active:
                    break
                if self.parent_order and self.parent_order.is_complete():
                    break

                # Check if passive order filled
                child = (
                    self.parent_order.child_orders.get(child_id)
                    if self.parent_order else None
                )
                if child and not child.is_terminal and child.leaves_qty > 0:
                    # Cancel passive, place aggressive
                    unfilled = child.leaves_qty
                    await self.cancel_child_order(child_id)

                    # Brief wait for cancel ack
                    await asyncio.sleep(0.01)

                    agg_id = await self._place_slice_order(
                        unfilled, aggressive=True
                    )
                    if agg_id:
                        self._current_child_id = agg_id
                    else:
                        self._residual += unfilled

                    logger.info(
                        "TWAP %s slice %d: escalated to aggressive, "
                        "qty=%.8f",
                        self.parent_order.parent_id if self.parent_order else "?",
                        i, unfilled,
                    )

                # --- Phase 3: Wait until slice end ---
                slice_end = self._algo_start_time + (i + 1) * self._slice_duration
                now = time.time()
                remaining_wait = slice_end - now
                if remaining_wait > 0:
                    await asyncio.sleep(remaining_wait)

                # Cancel anything still open at slice end
                if self._current_child_id and self.parent_order:
                    child = self.parent_order.child_orders.get(
                        self._current_child_id
                    )
                    if child and not child.is_terminal:
                        unfilled = child.leaves_qty
                        await self.cancel_child_order(self._current_child_id)
                        self._residual += unfilled
                        logger.info(
                            "TWAP %s slice %d: end-of-slice cancel, "
                            "residual+=%.8f",
                            self.parent_order.parent_id if self.parent_order else "?",
                            i, unfilled,
                        )
                    self._current_child_id = None

                logger.info(
                    "TWAP %s slice %d/%d done: filled=%.8f target=%.8f "
                    "jitter_delay=%.3fs",
                    self.parent_order.parent_id if self.parent_order else "?",
                    i + 1, self.num_slices,
                    self._filled_per_slice[i],
                    slice_target,
                    self._jittered_start_times[i] - (
                        self._algo_start_time + i * self._slice_duration
                    ),
                )

            # --- Residual sweep ---
            await self._sweep_residual()

            # --- Timer drift check ---
            self._check_timer_drift()

        except asyncio.CancelledError:
            logger.info(
                "TWAP %s scheduler cancelled",
                self.parent_order.parent_id if self.parent_order else "?",
            )
        except Exception as e:
            logger.error(
                "TWAP %s scheduler error: %s",
                self.parent_order.parent_id if self.parent_order else "?",
                e,
            )

    # --- Order placement ---

    async def _place_slice_order(
        self, qty: float, aggressive: bool = False
    ) -> str | None:
        """
        Place a child order for a slice.

        Passive: limit at near side of spread + offset.
        Aggressive: limit at far side of spread (cross the spread).
        """
        if self.parent_order is None:
            return None

        side = self.parent_order.side

        if aggressive:
            price = self._aggressive_price(side)
        else:
            price = self._passive_price(side)

        if price <= 0:
            # No valid market data; fall back to market order
            return await self.submit_child_order(
                qty=qty, price=0.0, ord_type=OrdType.Market
            )

        return await self.submit_child_order(
            qty=qty, price=price, ord_type=OrdType.Limit
        )

    def _passive_price(self, side: str) -> float:
        """Calculate passive limit price (near side of spread)."""
        if self._latest_mid <= 0:
            return 0.0
        offset = self.price_offset_bps * self._latest_mid / 10000.0
        if side == Side.Buy:
            return self._latest_bid + offset
        else:
            return self._latest_ask - offset

    def _aggressive_price(self, side: str) -> float:
        """Calculate aggressive limit price (cross the spread)."""
        if side == Side.Buy:
            return self._latest_ask if self._latest_ask > 0 else 0.0
        else:
            return self._latest_bid if self._latest_bid > 0 else 0.0

    # --- Residual handling ---

    async def _sweep_residual(self) -> None:
        """Sweep any residual quantity with a market order after all slices."""
        if self.parent_order is None:
            return
        if not self._active:
            return

        remaining = self.parent_order.remaining_qty()
        if remaining <= 0:
            return

        # Check timer drift — if past end time, sweep immediately
        now = time.time()
        if now > self._algo_end_time:
            logger.warning(
                "TWAP %s overran end time by %.1fs, sweeping residual %.8f",
                self.parent_order.parent_id,
                now - self._algo_end_time,
                remaining,
            )

        # Warn if residual is large
        if remaining > 0.2 * self.parent_order.total_qty:
            logger.warning(
                "TWAP %s residual %.8f is >20%% of total qty %.8f",
                self.parent_order.parent_id,
                remaining,
                self.parent_order.total_qty,
            )

        logger.info(
            "TWAP %s sweeping residual: qty=%.8f with market order",
            self.parent_order.parent_id,
            remaining,
        )

        await self.submit_child_order(
            qty=remaining, price=0.0, ord_type=OrdType.Market
        )

    # --- Timer drift ---

    def _check_timer_drift(self) -> None:
        """Log execution time vs planned horizon."""
        if self.parent_order is None:
            return
        now = time.time()
        actual_duration = now - self._algo_start_time
        drift = actual_duration - self.horizon_seconds
        logger.info(
            "TWAP %s timer: planned=%ds actual=%.1fs drift=%.1fs",
            self.parent_order.parent_id,
            self.horizon_seconds,
            actual_duration,
            drift,
        )

    # --- Jitter generation ---

    def _generate_jittered_times(self) -> list[float]:
        """
        Pre-calculate jittered start times for all slices.

        Each slice's start time is randomly offset by +/- jitter_pct of the
        slice duration. Clamps to ensure non-negative delay and no overlap
        (slice N+1 must start after slice N's jittered start).
        """
        times: list[float] = []
        for i in range(self.num_slices):
            base_start = self._algo_start_time + i * self._slice_duration
            jitter = self._slice_duration * random.uniform(
                -self.jitter_pct, self.jitter_pct
            )
            jittered = base_start + jitter

            # Clamp: must not be before algo start
            jittered = max(jittered, self._algo_start_time)

            # Clamp: must not overlap with previous slice's jittered start
            if times:
                # Ensure at least a tiny gap after the previous start
                jittered = max(jittered, times[-1] + 0.001)

            times.append(jittered)

        return times

    # --- Circuit breakers ---

    def _check_circuit_breakers(self) -> bool:
        """
        Check price and spread circuit breakers.

        Returns True if a circuit breaker is triggered (should pause).
        """
        if self._arrival_mid <= 0 or self._latest_mid <= 0:
            return False

        # Price circuit breaker: mid moved > threshold from arrival
        price_move = abs(self._latest_mid - self._arrival_mid) / self._arrival_mid
        if price_move > self.price_circuit_pct:
            logger.warning(
                "TWAP price circuit breaker: mid=%.4f arrival=%.4f "
                "move=%.2f%% > %.2f%%",
                self._latest_mid,
                self._arrival_mid,
                price_move * 100,
                self.price_circuit_pct * 100,
            )
            return True

        # Spread circuit breaker: spread > threshold of mid
        if self._latest_bid > 0 and self._latest_ask > 0:
            spread = self._latest_ask - self._latest_bid
            spread_pct = spread / self._latest_mid
            if spread_pct > self.spread_circuit_pct:
                logger.warning(
                    "TWAP spread circuit breaker: spread=%.4f mid=%.4f "
                    "spread_pct=%.2f%% > %.2f%%",
                    spread,
                    self._latest_mid,
                    spread_pct * 100,
                    self.spread_circuit_pct * 100,
                )
                return True

        return False
