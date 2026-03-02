# Risk Auditor Agent Report

## Date: 2026-03-01

## Summary

The risk control framework is in strong shape. All four pre-trade risk checks (max_order_qty, max_order_notional, max_position_qty, max_open_orders) are correctly applied on new orders. The amend path properly computes the net delta (not the absolute value). EXCHCONN now has defense-in-depth sanity checks on both new orders and amends, with the safety limit raised to 1,000,000 to avoid conflicting with per-symbol limits for high-denomination coins. The `_orders_lock` is now consistently acquired around all `self.orders` access. Several issues from the prior audit cycle have been fixed.

Remaining gaps: market orders bypass notional limits entirely, fill notifications to POSMANAGER are fire-and-forget (no retry/queue), direct EXCHCONN access bypasses all OM risk controls, and zero-value limits can silently halt all trading.

## Findings

### [MEDIUM] Market orders bypass notional limit check entirely

- **Category**: Order Validation / Edge Cases
- **Location**: `shared/risk_limits.py:77`
- **Issue**: `check_order()` only applies `max_order_notional` when `ord_type == ORD_TYPE_LIMIT and price > 0`. Market orders have `price=0` at submission (execution price is unknown), so the notional check is skipped. A market order for the full `max_order_qty` (e.g., 10 BTC worth ~$600k+) would pass despite `max_order_notional` being $100,000 (or $10 in current `risk_limits.json`).
- **Impact**: The dollar-value safety net is completely ineffective for market orders. The per-symbol `max_order_qty` provides some protection, but the notional limit -- specifically designed as a dollar cap -- does not apply.
- **Suggested Fix**: For market orders, estimate notional using the last known market price (OM could maintain a price cache from MKTDATA, or GUIBROKER could supply an indicative price). If no price is available, reject or apply a conservative fallback.

### [MEDIUM] Fill notifications to POSMANAGER are fire-and-forget with no retry

- **Category**: Position Tracking Consistency
- **Location**: `om/order_manager.py:614-627`
- **Issue**: When a fill occurs, OM sends a fill notification to POSMANAGER via `self.pos_client.send(fill_notification)`. If this fails (POSMANAGER disconnected, WebSocket error), the exception is caught at line 627, logged at ERROR, and silently dropped. The fill is not queued for retry. OM's internal `self._positions` has already been updated (line 607-608), but POSMANAGER never learns about the fill. Note: EXCHCONN has a `_pending_reports` deque with replay-on-reconnect for OM, but OM has no equivalent for POSMANAGER.
- **Impact**: OM and POSMANAGER position tracking permanently diverge after any dropped fill. OM's risk checks use its own (correct) position, but POSMANAGER reports stale positions and incorrect P&L to the GUI. The drift is invisible to the user and accumulates. No reconciliation mechanism exists.
- **Suggested Fix**: Implement a fill notification queue with replay-on-reconnect, mirroring EXCHCONN's `_pending_reports` pattern. Or add periodic reconciliation where POSMANAGER queries OM for current net positions.

### [MEDIUM] Direct connection to EXCHCONN (port 8084) bypasses all OM risk controls

- **Category**: Bypass Vectors
- **Location**: `exchconn/exchconn.py:48`
- **Issue**: EXCHCONN's WebSocket server on port 8084 accepts connections from any client with no authentication. Any process connecting to `ws://localhost:8084` can submit FIX orders directly to exchange simulators, bypassing all four OM risk checks. The only guard is EXCHCONN's own `MAX_ORDER_QTY=1,000,000` sanity check and qty > 0 validation.
- **Impact**: In a networked or multi-user environment, this is a complete risk control bypass. Even on localhost, a rogue script or misconfigured component could submit unchecked orders. Fills from direct EXCHCONN orders would not be tracked by OM or POSMANAGER, causing silent position drift and invisible risk exposure.
- **Suggested Fix**: Add token-based authentication on the EXCHCONN WebSocket handshake. Alternatively, bind to a Unix domain socket accessible only to OM. At minimum, validate connecting clients and log warnings for unexpected connections.

### [LOW] `save_limits()` allows zero-value limits that silently halt all trading

- **Category**: Risk Limit Integrity
- **Location**: `shared/risk_limits.py:44-52`
- **Issue**: Validation in `save_limits()` rejects `fv < 0` (strictly negative) but allows zero. Setting `max_open_orders` to 0 blocks all orders (since `open_order_count >= 0` is always true at `check_order` line 99). Setting `max_order_notional` to 0 blocks all limit orders. While intentional "kill switch" usage is possible, accidental zero values via GUI produce confusing rejections like "Open order count 0 has reached max 0".
- **Impact**: User could accidentally save zero values, rendering the system unable to accept any orders, with no clear indication of why.
- **Suggested Fix**: Reject zero values (`fv <= 0`), or add a GUI-side confirmation when saving zero-value limits.

### [LOW] `_validate_order` passes `self._positions` to `check_order` without holding `_positions_lock`

- **Category**: Position Tracking / Edge Cases
- **Location**: `om/order_manager.py:124`
- **Issue**: `_validate_order` is a synchronous (non-async) method that passes `self._positions` by reference to `check_order()`. It cannot acquire the async `_positions_lock` because doing so requires `await`. Since `_validate_order` runs synchronously in the asyncio event loop without yielding, the position dict cannot be mutated mid-check -- this is currently safe. However, if `_validate_order` is ever made async, positions could change between read and risk decision.
- **Impact**: No current runtime issue. Latent risk if the method is refactored to be async.
- **Suggested Fix**: Make `_validate_order` async and acquire `_positions_lock` before passing positions to `check_order`, or add a code comment documenting the synchronous invariant.

### [LOW] Amend path does not re-check `max_open_orders`

- **Category**: Order Validation
- **Location**: `om/order_manager.py:331-471`
- **Issue**: The amend path in `_handle_cancel_replace_request` checks max_order_qty (line 403-423), max_order_notional (line 425-443), and max_position_qty with delta (lines 445-471), but does not re-check max_open_orders. This omission is logically correct -- an amend does not create a new order, it modifies an existing one, so the open order count does not change. However, it is worth documenting this intentional omission for clarity.
- **Impact**: None. Correct behavior.
- **Suggested Fix**: Add a code comment on the amend risk section explaining why max_open_orders is intentionally omitted.

## Previously Fixed Issues (Verified Clean)

The following issues from the prior audit cycle have been resolved:

- **EXCHCONN MAX_ORDER_QTY conflict (was HIGH)**: `MAX_ORDER_QTY` raised from 1,000 to 1,000,000 (`exchconn/exchconn.py:34`), eliminating conflicts with per-symbol OM limits for ADA/USD (100k) and DOGE/USD (500k).
- **EXCHCONN amend path missing sanity check (was HIGH)**: Amend messages now undergo the same `qty <= 0` and `qty > MAX_ORDER_QTY` defense-in-depth checks (`exchconn/exchconn.py:134-146`).
- **`_orders_lock` not acquired (was LOW)**: The lock is now consistently used with `async with self._orders_lock` at 10 call sites covering all `self.orders` reads and writes.
- **Amend delta computation (was W-1)**: `_handle_cancel_replace_request` correctly computes `delta = new_qty - old_leaves` (line 452) and applies `signed_delta` based on side, matching the Learned Theme.
- **`save_limits()` schema validation (was I-3)**: `save_limits()` validates expected keys against an allowlist, checks per-symbol maps are dicts with non-negative values, and checks scalar limits are non-negative.

## Audit Scope Coverage

The following categories were audited and confirmed clean:

- **New order risk checks**: `_validate_order` (`om/order_manager.py:84-129`) calls `risk_limits.check_order()` applying all four risk checks with safe `.get()` access and `None` guards.
- **Risk limits file fallback**: `load_limits()` (`shared/risk_limits.py:18-26`) falls back to `copy.deepcopy(DEFAULT_RISK_LIMITS)` if file is missing or malformed.
- **Safe key access in `check_order()`**: All limit lookups use `.get()` with fallback. Per-symbol lookups check `is not None` before applying.
- **Position check uses absolute value**: `check_order()` uses `abs(projected)` at line 91, correctly bounding both long and short positions.
- **Risk limits re-read on every order**: Both `_validate_order` (line 117) and `_handle_cancel_replace_request` (line 404) call `risk_limits.load_limits()` fresh, ensuring runtime GUI edits take immediate effect.
- **Zero-quantity order rejection**: `_validate_order` rejects `qty <= 0` at line 94.
- **Negative price rejection**: Limit orders with `price <= 0` rejected at line 104-105. Market orders correctly accept `price=0`.
- **Unknown symbol rejection**: Orders for symbols not in `SYMBOLS` rejected at line 87-88.
- **Cancel order existence check**: Cancel requests validate original order exists (`om/order_manager.py:277-299`) and reject if not found.
- **Market order price safety**: Price initialized to `0.0` at line 98 and parsed with default `"0"` for market orders, eliminating unbound variable risk.
- **POSMANAGER fill validation**: `_process_fill` (`posmanager/posmanager.py:204-215`) validates symbol, side, qty > 0, and price > 0 before applying fills. Invalid fills are logged and dropped.
- **POSMANAGER position math**: The `Position` class correctly handles long-to-short and short-to-long crossings with proper avg_cost resets and realized P&L computation.
- **EXCHCONN defense-in-depth**: Both new order and amend paths validate qty > 0 and qty <= MAX_ORDER_QTY before routing to exchange adapters.
- **GUI risk limits API**: POST `/api/risk-limits` passes through `save_limits()` validation, catching `ValueError` and returning HTTP 400 on invalid input. GET endpoint serves current limits from file with safe fallback.
