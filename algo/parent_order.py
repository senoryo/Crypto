"""
Parent order state machine and child order tracking for algo execution.

A parent order represents a high-level algo instruction (e.g., "VWAP buy 10 BTC
over the next hour"). The algo engine decomposes it into child orders that flow
through the normal OM -> EXCHCONN pipeline.
"""

import time
from dataclasses import dataclass, field
from typing import Optional


# --- Parent order states ---
class ParentState:
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETING = "COMPLETING"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


# Valid state transitions
_VALID_TRANSITIONS: dict[str, set[str]] = {
    ParentState.PENDING: {ParentState.ACTIVE, ParentState.CANCELLED},
    ParentState.ACTIVE: {ParentState.PAUSED, ParentState.COMPLETING, ParentState.DONE, ParentState.CANCELLED},
    ParentState.PAUSED: {ParentState.ACTIVE, ParentState.CANCELLED},
    ParentState.COMPLETING: {ParentState.DONE, ParentState.CANCELLED},
    ParentState.DONE: set(),
    ParentState.CANCELLED: set(),
}


class InvalidStateTransition(Exception):
    """Raised when an invalid state transition is attempted."""


@dataclass
class ChildOrder:
    """Represents a child order spawned by a strategy."""
    cl_ord_id: str
    parent_id: str
    symbol: str
    side: str
    qty: float
    price: float
    ord_type: str
    exchange: str
    status: str = "PENDING_NEW"
    filled_qty: float = 0.0
    avg_px: float = 0.0
    created_at: float = field(default_factory=time.time)

    @property
    def is_terminal(self) -> bool:
        return self.status in ("FILLED", "CANCELLED", "REJECTED")

    @property
    def leaves_qty(self) -> float:
        return max(0.0, self.qty - self.filled_qty)


class ParentOrder:
    """
    State machine for a parent algo order.

    Tracks overall execution progress, child order mapping, and aggregate
    fill statistics (filled qty, average price, slippage).
    """

    def __init__(
        self,
        parent_id: str,
        symbol: str,
        side: str,
        total_qty: float,
        algo_type: str,
        params: Optional[dict] = None,
        arrival_price: float = 0.0,
    ):
        self.parent_id = parent_id
        self.symbol = symbol
        self.side = side
        self.total_qty = total_qty
        self.algo_type = algo_type
        self.state = ParentState.PENDING
        self.params = params or {}
        self.arrival_price = arrival_price

        # Child order tracking
        self.child_orders: dict[str, ChildOrder] = {}

        # Aggregate fill stats
        self.filled_qty: float = 0.0
        self.avg_fill_price: float = 0.0
        self._fill_notional: float = 0.0  # sum(fill_qty * fill_price) for WAVG calc

        # Timestamps
        self.created_at: float = time.time()
        self.started_at: float = 0.0
        self.completed_at: float = 0.0

    # --- State transitions ---

    def _transition(self, new_state: str) -> None:
        valid = _VALID_TRANSITIONS.get(self.state, set())
        if new_state not in valid:
            raise InvalidStateTransition(
                f"Cannot transition from {self.state} to {new_state}"
            )
        self.state = new_state

    def start(self) -> None:
        """Transition PENDING -> ACTIVE."""
        self._transition(ParentState.ACTIVE)
        self.started_at = time.time()

    def pause(self) -> None:
        """Transition ACTIVE -> PAUSED."""
        self._transition(ParentState.PAUSED)

    def resume(self) -> None:
        """Transition PAUSED -> ACTIVE."""
        self._transition(ParentState.ACTIVE)

    def begin_completing(self) -> None:
        """Transition ACTIVE -> COMPLETING."""
        self._transition(ParentState.COMPLETING)

    def complete(self) -> None:
        """Transition ACTIVE/COMPLETING -> DONE."""
        self._transition(ParentState.DONE)
        self.completed_at = time.time()

    def cancel(self) -> None:
        """Transition any non-terminal state -> CANCELLED."""
        if self.state in (ParentState.DONE, ParentState.CANCELLED):
            return  # Already terminal, idempotent
        self._transition(ParentState.CANCELLED)
        self.completed_at = time.time()

    # --- Child order management ---

    def add_child_order(self, child: ChildOrder) -> None:
        """Register a child order under this parent."""
        if child.cl_ord_id in self.child_orders:
            raise ValueError(f"Duplicate child order ID: {child.cl_ord_id}")
        self.child_orders[child.cl_ord_id] = child

    def process_fill(self, cl_ord_id: str, fill_qty: float, fill_price: float) -> None:
        """
        Process a fill on a child order, updating both child and parent aggregates.

        Args:
            cl_ord_id: The child order that received the fill.
            fill_qty: Quantity filled in this execution.
            fill_price: Price of this execution.
        """
        child = self.child_orders.get(cl_ord_id)
        if child is None:
            raise KeyError(f"Unknown child order: {cl_ord_id}")

        if fill_qty <= 0:
            return

        # Update child
        child.filled_qty += fill_qty
        # Weighted average for child
        old_notional = child.avg_px * (child.filled_qty - fill_qty)
        child.avg_px = (old_notional + fill_qty * fill_price) / child.filled_qty
        if child.filled_qty >= child.qty:
            child.status = "FILLED"
        else:
            child.status = "PARTIALLY_FILLED"

        # Update parent aggregates
        self._fill_notional += fill_qty * fill_price
        self.filled_qty += fill_qty
        if self.filled_qty > 0:
            self.avg_fill_price = self._fill_notional / self.filled_qty

    def process_child_new(self, cl_ord_id: str) -> None:
        """Mark a child order as acknowledged (New)."""
        child = self.child_orders.get(cl_ord_id)
        if child is None:
            raise KeyError(f"Unknown child order: {cl_ord_id}")
        child.status = "NEW"

    def process_child_cancelled(self, cl_ord_id: str) -> None:
        """Mark a child order as cancelled."""
        child = self.child_orders.get(cl_ord_id)
        if child is None:
            raise KeyError(f"Unknown child order: {cl_ord_id}")
        child.status = "CANCELLED"

    def process_reject(self, cl_ord_id: str, reason: str) -> None:
        """Mark a child order as rejected."""
        child = self.child_orders.get(cl_ord_id)
        if child is None:
            raise KeyError(f"Unknown child order: {cl_ord_id}")
        child.status = "REJECTED"

    # --- Aggregates ---

    def slippage(self) -> float:
        """
        Calculate slippage vs arrival price.
        Positive = cost (bought higher or sold lower than arrival).
        Returns 0 if no fills or no arrival price.
        """
        if self.filled_qty == 0 or self.arrival_price == 0:
            return 0.0
        price_diff = self.avg_fill_price - self.arrival_price
        # For buys, paying more is positive slippage (cost).
        # For sells, receiving less is positive slippage (cost).
        # Side "1" = Buy, "2" = Sell
        sign = 1.0 if self.side == "1" else -1.0
        return price_diff * sign * self.filled_qty

    def fill_pct(self) -> float:
        """Percentage of total quantity filled (0.0 to 1.0)."""
        if self.total_qty <= 0:
            return 0.0
        return min(self.filled_qty / self.total_qty, 1.0)

    def is_complete(self) -> bool:
        """Whether filled quantity meets or exceeds total quantity (within tolerance)."""
        tolerance = self.total_qty * 1e-9
        return self.filled_qty >= (self.total_qty - tolerance)

    def remaining_qty(self) -> float:
        """Quantity still to be filled."""
        return max(0.0, self.total_qty - self.filled_qty)

    def active_child_count(self) -> int:
        """Number of non-terminal child orders."""
        return sum(
            1 for c in self.child_orders.values() if not c.is_terminal
        )

    def to_dict(self) -> dict:
        """Serialize parent order state to a dictionary."""
        return {
            "parent_id": self.parent_id,
            "symbol": self.symbol,
            "side": self.side,
            "total_qty": self.total_qty,
            "algo_type": self.algo_type,
            "state": self.state,
            "params": self.params,
            "arrival_price": self.arrival_price,
            "filled_qty": self.filled_qty,
            "avg_fill_price": self.avg_fill_price,
            "fill_pct": self.fill_pct(),
            "slippage": self.slippage(),
            "remaining_qty": self.remaining_qty(),
            "active_children": self.active_child_count(),
            "total_children": len(self.child_orders),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }
