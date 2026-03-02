"""
Implementation Shortfall (IS) execution strategy.

Minimizes total execution cost relative to the arrival price (decision price)
using the Almgren-Chriss framework. Balances market impact (trading too fast)
against timing risk (adverse drift from trading too slow).

Key features:
- Optimal execution trajectory based on urgency and risk aversion
- Adaptive re-optimization based on real-time price drift
- IS cost decomposition: delay cost, impact cost, timing cost
- Circuit breakers for price drift, spread, and slippage
"""

import asyncio
import logging
import math
import time

from algo.parent_order import ChildOrder, ParentOrder, ParentState
from algo.strategies.base import BaseStrategy
from shared.fix_protocol import OrdType, Side

logger = logging.getLogger("ALGO")


class ISStrategy(BaseStrategy):
    """
    Implementation Shortfall strategy.

    Uses the Almgren-Chriss framework to compute an optimal execution
    trajectory that minimizes E[cost] + lambda * Var[cost]. The urgency
    parameter controls front-loading: 0 = uniform (TWAP-like), 1 = immediate.
    """

    STRATEGY_NAME = "IS"

    def __init__(self, engine, params: dict | None = None):
        super().__init__(engine)
        params = params or {}

        # Execution window
        self.horizon_seconds: float = params.get("horizon_seconds", 3600)
        self.num_buckets: int = params.get("num_buckets", 20)

        # Almgren-Chriss parameters
        self.urgency: float = params.get("urgency", 0.5)
        self.risk_aversion: float = params.get("risk_aversion", 1e-6)
        self.temporary_impact_coeff: float = params.get("temporary_impact_coeff", 0.1)
        self.permanent_impact_coeff: float = params.get("permanent_impact_coeff", 0.01)
        self.volatility: float = params.get("volatility", 100)

        # Adaptive mode
        self.adaptive: bool = params.get("adaptive", True)

        # Order placement
        self.price_offset_bps: float = params.get("price_offset_bps", 5)
        self.aggressive_threshold: float = params.get("aggressive_threshold", 0.6)

        # Circuit breaker thresholds
        self.max_price_drift_pct: float = params.get("max_price_drift_pct", 5.0)
        self.max_spread_pct: float = params.get("max_spread_pct", 2.0)
        self.max_slippage_pct: float = params.get("max_slippage_pct", 1.0)

        # Internal state
        self._arrival_price: float = 0.0
        self._latest_bid: float = 0.0
        self._latest_ask: float = 0.0
        self._latest_mid: float = 0.0
        self._bucket_targets: list[float] = []
        self._bucket_filled: list[float] = []
        self._current_bucket: int = 0
        self._scheduler_task: asyncio.Task | None = None
        self._total_fill_qty: float = 0.0
        self._total_fill_notional: float = 0.0
        self._first_fill_price: float = 0.0
        self._active_child_ids: set[str] = set()
        self._bucket_start_price: float = 0.0

    # --- Lifecycle ---

    async def on_start(self) -> None:
        """Validate parameters, capture arrival price, compute trajectory, start scheduler."""
        if self.urgency < 0.0 or self.urgency > 1.0:
            logger.error("IS urgency must be in [0, 1], got %s", self.urgency)
            self._active = False
            if self.parent_order:
                self.parent_order.cancel()
            return

        if self.parent_order is None:
            return

        # Capture arrival price
        self._arrival_price = self.parent_order.arrival_price
        logger.info(
            "IS started: parent=%s symbol=%s qty=%s arrival_px=%s "
            "urgency=%s risk_aversion=%s horizon=%ss buckets=%s adaptive=%s",
            self.parent_order.parent_id,
            self.parent_order.symbol,
            self.parent_order.total_qty,
            self._arrival_price,
            self.urgency,
            self.risk_aversion,
            self.horizon_seconds,
            self.num_buckets,
            self.adaptive,
        )

        # Set initial mid from arrival
        if self._arrival_price > 0:
            self._latest_mid = self._arrival_price

        # Compute initial trajectory
        self._compute_trajectory()

        # Initialize bucket fill tracking
        self._bucket_filled = [0.0] * self.num_buckets

        # Start bucket scheduler
        self._scheduler_task = asyncio.ensure_future(self._run_scheduler())

    async def stop(self) -> None:
        """Cancel scheduler and all child orders."""
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        await super().stop()

    # --- Market data ---

    async def on_tick(self, symbol: str, market_data: dict) -> None:
        """Update latest bid/ask/mid and track price drift."""
        if not self._active or self._paused:
            return
        if self.parent_order is None:
            return
        if symbol != self.parent_order.symbol:
            return

        bid = market_data.get("bid", market_data.get("price", 0))
        ask = market_data.get("ask", market_data.get("price", 0))

        if bid and float(bid) > 0:
            self._latest_bid = float(bid)
        if ask and float(ask) > 0:
            self._latest_ask = float(ask)
        if self._latest_bid > 0 and self._latest_ask > 0:
            self._latest_mid = (self._latest_bid + self._latest_ask) / 2.0
        elif self._latest_bid > 0:
            self._latest_mid = self._latest_bid
        elif self._latest_ask > 0:
            self._latest_mid = self._latest_ask

    # --- Execution reports ---

    async def on_fill(self, child_order: ChildOrder, fill_qty: float, fill_price: float) -> None:
        """Track fills, update IS cost, check completion."""
        self._total_fill_qty += fill_qty
        self._total_fill_notional += fill_qty * fill_price

        if self._first_fill_price == 0.0:
            self._first_fill_price = fill_price

        # Track per-bucket fills
        if 0 <= self._current_bucket < len(self._bucket_filled):
            self._bucket_filled[self._current_bucket] += fill_qty

        # Remove from active set if terminal
        if child_order.is_terminal:
            self._active_child_ids.discard(child_order.cl_ord_id)

        is_cost = self._calculate_is_cost()
        logger.info(
            "IS fill: child=%s qty=%s px=%s total_filled=%s/%s IS_cost=%.4f",
            child_order.cl_ord_id,
            fill_qty,
            fill_price,
            self._total_fill_qty,
            self.parent_order.total_qty if self.parent_order else "?",
            is_cost.get("total_is", 0.0),
        )

        # Check completion
        if self.parent_order and self.parent_order.is_complete():
            self._active = False
            self.parent_order.complete()
            if self._scheduler_task and not self._scheduler_task.done():
                self._scheduler_task.cancel()
            logger.info(
                "IS completed: parent=%s filled=%s avg_px=%s IS=%s",
                self.parent_order.parent_id,
                self.parent_order.filled_qty,
                self.parent_order.avg_fill_price,
                is_cost,
            )

    async def on_reject(self, child_order: ChildOrder, reason: str) -> None:
        """Add rejected qty back to current/next bucket."""
        logger.warning(
            "IS child rejected: %s reason=%s",
            child_order.cl_ord_id,
            reason,
        )
        self._active_child_ids.discard(child_order.cl_ord_id)

        # Return unfilled qty to current bucket target
        unfilled = child_order.qty - child_order.filled_qty
        if unfilled > 0 and self._current_bucket < len(self._bucket_targets):
            bucket = min(self._current_bucket, len(self._bucket_targets) - 1)
            self._bucket_targets[bucket] += unfilled

    async def on_cancel_ack(self, child_order: ChildOrder) -> None:
        """Track cancelled qty."""
        self._active_child_ids.discard(child_order.cl_ord_id)
        logger.info("IS child cancelled: %s", child_order.cl_ord_id)

    # --- Scheduler ---

    async def _run_scheduler(self) -> None:
        """
        Execute buckets sequentially over the horizon.

        For each bucket:
        1. Calculate target qty from trajectory
        2. If adaptive: re-optimize remaining trajectory
        3. Place order (passive first, escalate if needed)
        4. Wait for bucket end
        5. Cancel unfilled, carry residual to next bucket
        Final sweep: market order for any remaining qty.
        """
        if self.parent_order is None:
            return

        bucket_duration = self.horizon_seconds / self.num_buckets

        try:
            for bucket_idx in range(self.num_buckets):
                if not self._active:
                    return

                self._current_bucket = bucket_idx

                # Wait while paused
                while self._paused and self._active:
                    await asyncio.sleep(0.1)
                if not self._active:
                    return

                # Check completion
                if self.parent_order.is_complete():
                    return

                # Adaptive trajectory re-optimization
                if self.adaptive and bucket_idx > 0:
                    self._adapt_trajectory(bucket_idx)

                # Get target for this bucket
                target_qty = self._bucket_targets[bucket_idx]
                remaining_parent = self.parent_order.remaining_qty()
                target_qty = min(target_qty, remaining_parent)

                if target_qty <= 0:
                    logger.info(
                        "IS bucket %d/%d: target=0, skipping",
                        bucket_idx + 1, self.num_buckets,
                    )
                    await asyncio.sleep(bucket_duration)
                    continue

                # Record bucket start price for drift tracking
                self._bucket_start_price = self._latest_mid

                # Circuit breaker checks
                if self._check_circuit_breakers():
                    logger.warning(
                        "IS bucket %d: circuit breaker triggered, pausing",
                        bucket_idx + 1,
                    )
                    await self.pause()
                    await asyncio.sleep(bucket_duration)
                    # Carry residual to next bucket
                    if bucket_idx + 1 < len(self._bucket_targets):
                        self._bucket_targets[bucket_idx + 1] += target_qty
                    continue

                logger.info(
                    "IS bucket %d/%d: target_qty=%.6f mid=%.2f drift_from_arrival=%.4f",
                    bucket_idx + 1,
                    self.num_buckets,
                    target_qty,
                    self._latest_mid,
                    self._price_drift_bps(),
                )

                # Place passive order first
                await self._place_bucket_order(target_qty, aggressive=False)

                # Wait for part of the bucket, then check if we need to escalate
                aggressive_wait = bucket_duration * self.aggressive_threshold
                await asyncio.sleep(aggressive_wait)

                if not self._active:
                    return

                # Check how much was filled in this bucket
                bucket_filled = self._bucket_filled[bucket_idx]
                unfilled = target_qty - bucket_filled

                if unfilled > 0 and not self.parent_order.is_complete():
                    # Cancel passive orders and place aggressive
                    await self._cancel_active_children()
                    if unfilled > 0:
                        await self._place_bucket_order(unfilled, aggressive=True)

                # Wait remaining bucket time
                remaining_time = bucket_duration * (1.0 - self.aggressive_threshold)
                await asyncio.sleep(remaining_time)

                # Cancel any still-active children from this bucket
                await self._cancel_active_children()

                # Carry unfilled residual to next bucket
                bucket_filled = self._bucket_filled[bucket_idx]
                residual = target_qty - bucket_filled
                if residual > 0 and bucket_idx + 1 < len(self._bucket_targets):
                    self._bucket_targets[bucket_idx + 1] += residual
                    logger.info(
                        "IS bucket %d: carrying residual %.6f to next bucket",
                        bucket_idx + 1, residual,
                    )

            # Final sweep: market order for any remaining qty
            if self._active and self.parent_order and not self.parent_order.is_complete():
                remaining = self.parent_order.remaining_qty()
                if remaining > 0:
                    logger.info("IS final sweep: remaining=%.6f", remaining)
                    await self._place_sweep_order(remaining)

        except asyncio.CancelledError:
            logger.info("IS scheduler cancelled")
            raise

    async def _place_bucket_order(self, qty: float, aggressive: bool = False) -> None:
        """Place an order for the current bucket."""
        if self.parent_order is None or qty <= 0:
            return

        if aggressive:
            # Cross the spread
            if self.parent_order.side == Side.Buy:
                price = self._latest_ask if self._latest_ask > 0 else self._latest_mid
            else:
                price = self._latest_bid if self._latest_bid > 0 else self._latest_mid
        else:
            # Passive: offset from mid
            offset = self._latest_mid * self.price_offset_bps / 10000.0
            if self.parent_order.side == Side.Buy:
                price = self._latest_mid - offset
            else:
                price = self._latest_mid + offset

        if price <= 0:
            # No valid price data, use market order
            cl_ord_id = await self.submit_child_order(
                qty=qty,
                ord_type=OrdType.Market,
            )
        else:
            cl_ord_id = await self.submit_child_order(
                qty=qty,
                price=price,
                ord_type=OrdType.Limit,
            )

        if cl_ord_id:
            self._active_child_ids.add(cl_ord_id)

    async def _place_sweep_order(self, qty: float) -> None:
        """Place a market order to sweep remaining quantity."""
        if self.parent_order is None or qty <= 0:
            return
        cl_ord_id = await self.submit_child_order(
            qty=qty,
            ord_type=OrdType.Market,
        )
        if cl_ord_id:
            self._active_child_ids.add(cl_ord_id)

    async def _cancel_active_children(self) -> None:
        """Cancel all currently active child orders."""
        for cl_ord_id in list(self._active_child_ids):
            await self.cancel_child_order(cl_ord_id)

    # --- Trajectory computation ---

    def _compute_trajectory(self) -> None:
        """
        Compute the Almgren-Chriss optimal execution trajectory.

        Uses urgency to control front-loading:
        - urgency=0 -> uniform distribution (TWAP-like)
        - urgency=1 -> heavily front-loaded
        """
        if self.parent_order is None:
            self._bucket_targets = []
            return

        total_qty = self.parent_order.total_qty

        kappa = self.urgency * 3.0  # Scale factor (0 = flat, 3 = very front-loaded)
        if kappa < 0.01:
            # Nearly flat -> uniform
            weights = [1.0 / self.num_buckets] * self.num_buckets
        else:
            # Exponential front-loading
            weights = []
            for i in range(self.num_buckets):
                t = i / self.num_buckets
                w = math.exp(-kappa * t)
                weights.append(w)
            # Normalize
            total_w = sum(weights)
            weights = [w / total_w for w in weights]

        self._bucket_targets = [total_qty * w for w in weights]

    def _adapt_trajectory(self, current_bucket: int) -> None:
        """
        Re-optimize remaining trajectory based on price drift from arrival.

        If price moved favorably (lower for BUY, higher for SELL):
          - Slow down: scale remaining targets by factor < 1.0
        If price moved adversely:
          - Speed up: scale remaining targets by factor > 1.0
        """
        if self.parent_order is None:
            return
        if self._arrival_price <= 0 or self._latest_mid <= 0:
            return

        drift_pct = (self._latest_mid - self._arrival_price) / self._arrival_price

        # Determine favorability based on side
        if self.parent_order.side == Side.Buy:
            # For BUY: negative drift (price dropped) is favorable
            favorability = -drift_pct
        else:
            # For SELL: positive drift (price rose) is favorable
            favorability = drift_pct

        # Compute adaptive factor
        if favorability > 0:
            # Price moved favorably -> slow down
            adaptive_factor = 1.0 - favorability * 0.3
        else:
            # Price moved adversely -> speed up
            adaptive_factor = 1.0 + abs(favorability) * 0.3

        # Clamp to prevent oscillation
        adaptive_factor = max(0.5, min(2.0, adaptive_factor))

        # Apply to remaining buckets
        remaining_qty = self.parent_order.remaining_qty()
        if remaining_qty <= 0:
            return

        remaining_buckets = self.num_buckets - current_bucket
        if remaining_buckets <= 0:
            return

        # Scale remaining bucket targets
        for i in range(current_bucket, self.num_buckets):
            self._bucket_targets[i] *= adaptive_factor

        # Re-normalize so remaining targets sum to remaining qty
        remaining_target_sum = sum(self._bucket_targets[current_bucket:])
        if remaining_target_sum > 0:
            scale = remaining_qty / remaining_target_sum
            for i in range(current_bucket, self.num_buckets):
                self._bucket_targets[i] *= scale

        logger.info(
            "IS adaptive: bucket=%d drift_pct=%.4f favorability=%.4f "
            "adaptive_factor=%.3f remaining_qty=%.6f",
            current_bucket,
            drift_pct,
            favorability,
            adaptive_factor,
            remaining_qty,
        )

    # --- IS cost calculation ---

    def _calculate_is_cost(self) -> dict:
        """
        Calculate Implementation Shortfall cost decomposition.

        Returns:
            dict with total_is, delay_cost, impact_cost, timing_cost
        """
        if self.parent_order is None or self._arrival_price <= 0:
            return {"total_is": 0.0, "delay_cost": 0.0, "impact_cost": 0.0, "timing_cost": 0.0}

        if self._total_fill_qty <= 0:
            return {"total_is": 0.0, "delay_cost": 0.0, "impact_cost": 0.0, "timing_cost": 0.0}

        avg_fill = self._total_fill_notional / self._total_fill_qty
        sign = 1.0 if self.parent_order.side == Side.Buy else -1.0

        # Total IS = (avg_fill - arrival) * sign * filled_qty
        total_is = (avg_fill - self._arrival_price) * sign * self._total_fill_qty

        # Delay cost: drift between arrival and first fill
        if self._first_fill_price > 0:
            delay_cost = (self._first_fill_price - self._arrival_price) * sign * self._total_fill_qty
        else:
            delay_cost = 0.0

        # Impact cost: estimated from our trading rate using temp impact model
        # Approximate as remaining IS after removing delay cost
        timing_cost = 0.0
        if self._latest_mid > 0 and self._arrival_price > 0:
            # Timing cost: unfilled qty exposed to current price drift
            remaining = self.parent_order.remaining_qty()
            timing_cost = (self._latest_mid - self._arrival_price) * sign * remaining

        impact_cost = total_is - delay_cost - timing_cost

        return {
            "total_is": total_is,
            "delay_cost": delay_cost,
            "impact_cost": impact_cost,
            "timing_cost": timing_cost,
        }

    # --- Circuit breakers ---

    def _check_circuit_breakers(self) -> bool:
        """
        Check all circuit breakers. Returns True if any triggered.

        Checks:
        - Price drift from arrival > max_price_drift_pct
        - Spread > max_spread_pct of mid
        - Running slippage > max_slippage_pct of notional
        """
        if self._arrival_price <= 0:
            return False

        # Price drift check
        if self._latest_mid > 0:
            drift_pct = abs(self._latest_mid - self._arrival_price) / self._arrival_price * 100
            if drift_pct > self.max_price_drift_pct:
                logger.warning(
                    "IS circuit breaker: price drift %.2f%% > %.2f%%",
                    drift_pct, self.max_price_drift_pct,
                )
                return True

        # Spread check
        if self._latest_bid > 0 and self._latest_ask > 0 and self._latest_mid > 0:
            spread_pct = (self._latest_ask - self._latest_bid) / self._latest_mid * 100
            if spread_pct > self.max_spread_pct:
                logger.warning(
                    "IS circuit breaker: spread %.2f%% > %.2f%%",
                    spread_pct, self.max_spread_pct,
                )
                return True

        # Slippage check
        if self._total_fill_qty > 0 and self.parent_order:
            is_cost = self._calculate_is_cost()
            notional = self._arrival_price * self.parent_order.total_qty
            if notional > 0:
                slippage_pct = abs(is_cost["total_is"]) / notional * 100
                if slippage_pct > self.max_slippage_pct:
                    logger.warning(
                        "IS circuit breaker: slippage %.2f%% > %.2f%%",
                        slippage_pct, self.max_slippage_pct,
                    )
                    return True

        return False

    # --- Helpers ---

    def _price_drift_bps(self) -> float:
        """Calculate price drift from arrival in basis points."""
        if self._arrival_price <= 0 or self._latest_mid <= 0:
            return 0.0
        return (self._latest_mid - self._arrival_price) / self._arrival_price * 10000
