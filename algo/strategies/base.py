"""
Abstract base strategy class for all execution algorithms.

Every strategy (VWAP, TWAP, IS, SOR) inherits from BaseStrategy and implements
the lifecycle and event hooks. The base class provides child order submission
and cancellation through the engine, as well as common bookkeeping.
"""

import itertools
import logging
from abc import ABC, abstractmethod

from algo.parent_order import ParentOrder, ChildOrder
from shared.fix_protocol import OrdType, Side

logger = logging.getLogger("ALGO")


class BaseStrategy(ABC):
    """Abstract base class for all execution strategies."""

    STRATEGY_NAME = "BASE"  # Override in subclasses

    def __init__(self, engine):
        self.engine = engine  # Reference to AlgoEngine
        self.parent_order: ParentOrder | None = None
        self._child_counter = itertools.count(1)
        self._active = False
        self._paused = False

    # --- Lifecycle ---

    async def start(self, parent_order: ParentOrder) -> None:
        """Called when algo should begin execution."""
        self.parent_order = parent_order
        self._active = True
        self._paused = False
        self.parent_order.start()
        await self.on_start()

    @abstractmethod
    async def on_start(self) -> None:
        """Strategy-specific startup logic. Implemented by subclasses."""

    async def stop(self) -> None:
        """Cancel the algo and all outstanding child orders."""
        self._active = False
        self._paused = False
        await self.cancel_all_children()
        if self.parent_order:
            self.parent_order.cancel()

    async def pause(self) -> None:
        """Temporarily halt execution (no new orders, keep existing)."""
        if not self._active or self._paused:
            return
        self._paused = True
        if self.parent_order:
            self.parent_order.pause()

    async def resume(self) -> None:
        """Resume execution after a pause."""
        if not self._paused:
            return
        self._paused = False
        if self.parent_order:
            self.parent_order.resume()

    # --- Market Data ---

    async def on_tick(self, symbol: str, market_data: dict) -> None:
        """Called on every market data update for subscribed symbols.
        Default no-op; override in subclasses that need tick-level signals.
        """

    # --- Execution Reports ---

    async def on_fill(self, child_order: ChildOrder, fill_qty: float, fill_price: float) -> None:
        """Called when a child order receives a fill. Override for strategy-specific logic."""

    async def on_reject(self, child_order: ChildOrder, reason: str) -> None:
        """Called when a child order is rejected. Override for strategy-specific logic."""

    async def on_cancel_ack(self, child_order: ChildOrder) -> None:
        """Called when a child order cancellation is confirmed. Override for strategy-specific logic."""

    # --- Order Submission ---

    async def submit_child_order(
        self,
        qty: float,
        price: float = 0.0,
        ord_type: str = OrdType.Limit,
        exchange: str = "",
    ) -> str | None:
        """
        Submit a child order through the engine.

        Returns the child cl_ord_id on success, or None if the engine
        rejected it (e.g., rate limit, paused, inactive).
        """
        if not self._active:
            logger.warning("Cannot submit child order: strategy is not active")
            return None
        if self._paused:
            logger.warning("Cannot submit child order: strategy is paused")
            return None
        if self.parent_order is None:
            logger.warning("Cannot submit child order: no parent order")
            return None

        cl_ord_id = self._next_child_id()
        child = ChildOrder(
            cl_ord_id=cl_ord_id,
            parent_id=self.parent_order.parent_id,
            symbol=self.parent_order.symbol,
            side=self.parent_order.side,
            qty=qty,
            price=price,
            ord_type=ord_type,
            exchange=exchange,
        )
        self.parent_order.add_child_order(child)
        sent = await self.engine.send_child_order(child)
        if not sent:
            child.status = "REJECTED"
            return None
        return cl_ord_id

    async def cancel_child_order(self, cl_ord_id: str) -> None:
        """Cancel an outstanding child order."""
        if self.parent_order is None:
            return
        child = self.parent_order.child_orders.get(cl_ord_id)
        if child and not child.is_terminal:
            await self.engine.cancel_child_order(child)

    async def cancel_all_children(self) -> None:
        """Cancel all non-terminal child orders for this strategy."""
        if self.parent_order is None:
            return
        for child in list(self.parent_order.child_orders.values()):
            if not child.is_terminal:
                await self.engine.cancel_child_order(child)

    def _next_child_id(self) -> str:
        n = next(self._child_counter)
        pid = self.parent_order.parent_id if self.parent_order else "NONE"
        return f"ALGO-{self.STRATEGY_NAME}-{pid}-{n}"
