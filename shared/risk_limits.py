"""
Risk limits: load, save, and pre-trade check functions.

Limits are persisted in a JSON file (risk_limits.json) and re-read on every
order check so GUI edits take effect immediately without restart.
"""

import copy
import json
import logging
import os

from shared.config import RISK_LIMITS_FILE, DEFAULT_RISK_LIMITS, SIDE_BUY, ORD_TYPE_LIMIT

logger = logging.getLogger("risk_limits")


def load_limits() -> dict:
    """Load risk limits from JSON file, falling back to defaults."""
    if os.path.exists(RISK_LIMITS_FILE):
        try:
            with open(RISK_LIMITS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to read {RISK_LIMITS_FILE}, using defaults: {e}")
    return copy.deepcopy(DEFAULT_RISK_LIMITS)


def save_limits(limits: dict) -> None:
    """Write risk limits to JSON file after basic schema validation."""
    # Validate expected keys and types
    _EXPECTED_KEYS = {"max_order_qty", "max_order_notional", "max_position_qty", "max_open_orders"}
    for key in limits:
        if key not in _EXPECTED_KEYS:
            raise ValueError(f"Unexpected risk limit key: {key}")

    for key in ("max_order_qty", "max_position_qty"):
        val = limits.get(key)
        if val is not None:
            if not isinstance(val, dict):
                raise ValueError(f"{key} must be a dict mapping symbol to limit value")
            for sym, v in val.items():
                fv = float(v)
                if fv < 0:
                    raise ValueError(f"{key}[{sym}] must be non-negative, got {v}")

    for key in ("max_order_notional", "max_open_orders"):
        val = limits.get(key)
        if val is not None:
            fv = float(val)
            if fv < 0:
                raise ValueError(f"{key} must be non-negative, got {val}")

    with open(RISK_LIMITS_FILE, "w") as f:
        json.dump(limits, f, indent=2)


def check_order(
    limits: dict,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    ord_type: str,
    positions: dict[str, float],
    open_order_count: int,
) -> str | None:
    """Return a reject reason string if the order breaches any risk limit, else None."""

    # Check 1: max order quantity per symbol
    max_qty_map = limits.get("max_order_qty", {})
    max_qty = max_qty_map.get(symbol)
    if max_qty is not None and qty > float(max_qty):
        return f"Order qty {qty} exceeds max {max_qty} for {symbol}"

    # Check 2: max order notional (limit orders only)
    if ord_type == ORD_TYPE_LIMIT and price > 0:
        max_notional = limits.get("max_order_notional")
        if max_notional is not None:
            notional = qty * price
            if notional > float(max_notional):
                return f"Order notional ${notional:,.2f} exceeds max ${float(max_notional):,.2f}"

    # Check 3: max position size per symbol
    max_pos_map = limits.get("max_position_qty", {})
    max_pos = max_pos_map.get(symbol)
    if max_pos is not None:
        current_pos = positions.get(symbol, 0.0)
        signed_qty = qty if side == SIDE_BUY else -qty
        projected = current_pos + signed_qty
        if abs(projected) > float(max_pos):
            return (
                f"Projected position {projected:+.6g} exceeds max "
                f"{float(max_pos)} for {symbol}"
            )

    # Check 4: max open orders (global)
    max_open = limits.get("max_open_orders")
    if max_open is not None and open_order_count >= int(max_open):
        return f"Open order count {open_order_count} has reached max {int(max_open)}"

    return None
