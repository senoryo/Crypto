# Risk Auditor Agent Report

## Mission
Audit all order paths for risk check coverage, identify gaps where orders could bypass risk controls, and verify position tracking consistency.

## Files Audited
- `om/order_manager.py` — Order validation, risk checking, order book management
- `shared/risk_limits.py` — Risk limit loading, persistence, and pre-trade check logic
- `shared/config.py` — Default risk limits, constants, routing
- `shared/fix_protocol.py` — FIX message construction (for understanding what fields are set)
- `posmanager/posmanager.py` — Position and P&L tracking from fills
- `exchconn/exchconn.py` — Exchange connectivity (potential bypass point)
- `exchconn/binance_sim.py` — Binance simulator (fill generation)
- `exchconn/coinbase_sim.py` — Coinbase simulator (fill generation)
- `guibroker/guibroker.py` — GUI-to-FIX bridge (order origination)
- `gui/server.py` — REST endpoint for risk limit editing
- `shared/ws_transport.py` — WebSocket transport reliability

## Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 0     |
| WARNING  | 1     |
| INFO     | 3     |
| PASSED   | 15    |

---

## Passed Checks (15)

1. **[PASS] new-order-risk-coverage** — `om/order_manager.py:83-123`
   `_validate_order` calls `risk_limits.check_order()` which applies all four risk checks: `max_order_qty`, `max_order_notional`, `max_position_qty`, and `max_open_orders`.

2. **[PASS] risk-limits-fallback** — `shared/risk_limits.py:17-25`
   `load_limits()` correctly falls back to `DEFAULT_RISK_LIMITS` when the JSON file is missing or malformed. Both `json.JSONDecodeError` and `OSError` are caught.

3. **[PASS] risk-limits-safe-key-access** — `shared/risk_limits.py:47-76`
   `check_order()` uses `.get()` with fallback for every limits key. No `KeyError` is possible. Per-symbol lookups use `.get(symbol)` and check `is not None` before applying.

4. **[PASS] position-check-abs-value** — `shared/risk_limits.py:63-70`
   Position limit check uses `abs(projected)` so both long and short positions are properly bounded.

5. **[PASS] notional-limit-orders-only** — `shared/risk_limits.py:53-58`
   Notional check is guarded by `if ord_type == ORD_TYPE_LIMIT and price > 0`, correctly skipping market orders where price is unknown at submission time.

6. **[PASS] risk-limits-re-read-every-order** — `om/order_manager.py:111`, `om/order_manager.py:362`
   Both `_validate_order` (new orders) and `_handle_cancel_replace_request` (amends) call `risk_limits.load_limits()` fresh, so GUI runtime edits take effect immediately.

7. **[PASS] zero-qty-rejection** — `om/order_manager.py:93-94`
   Orders with qty <= 0 are rejected: `if qty <= 0: return "Quantity must be positive..."`.

8. **[PASS] negative-price-rejection** — `om/order_manager.py:103-104`
   Limit orders with price <= 0 are rejected: `if price <= 0: return "Limit order price must be positive..."`.

9. **[PASS] unknown-symbol-rejection** — `om/order_manager.py:86-87`
   Orders for symbols not in `SYMBOLS` list are rejected immediately.

10. **[PASS] cancel-path-validation** — `om/order_manager.py:251-274`
    Cancel requests validate that the original order exists in the order book before forwarding. Unknown orders receive a reject execution report.

11. **[PASS] amend-qty-validation** — `om/order_manager.py:316-341`
    Amend path rejects qty <= 0 and invalid prices for limit orders.

12. **[PASS] amend-max-order-qty-check** — `om/order_manager.py:362-381`
    Amend path applies `max_order_qty` check against amended quantity.

13. **[PASS] amend-max-notional-check** — `om/order_manager.py:383-401`
    Amend path applies `max_order_notional` check against amended qty * price for limit orders.

14. **[PASS] amend-position-limit-check** — `om/order_manager.py:403-427`
    Amend path applies `max_position_qty` check with projected position calculation using the positions lock.

15. **[PASS] market-order-price-safe** — `om/order_manager.py:97`
    Price is initialized to `float(fix_msg.get(Tag.Price, "0"))` before the `if ord_type == OrdType.Limit` branch, so it is always bound regardless of order type. No unbound variable risk.

---

## Warnings (1)

### W-1: Amend position limit check does not subtract the old order's pending qty
**File:** `om/order_manager.py:403-427`
**Severity:** WARNING

When checking position limits on an amend, the code projects position as `current_pos + signed_new_qty`. However, the original order already has pending (unfilled) quantity contributing to the position risk. The correct projection should account for the delta: the new amended qty replaces the old qty, so the check should subtract the old order's remaining leaves_qty from the projected position before adding the new qty.

Currently, if an order for BUY 5 BTC is amended to BUY 6 BTC, the position check treats it as an additional 6 BTC on top of whatever is already filled, rather than a net change of +1 BTC from the amendment. This makes the check overly conservative (rejecting valid amends) but does not allow risk bypass, so it is a WARNING rather than CRITICAL.

**Recommendation:** Calculate the net delta as `(new_qty - old_leaves_qty)` and project based on that.

---

## Informational (3)

### I-1: EXCHCONN accepts orders from any WebSocket client without authentication
**File:** `exchconn/exchconn.py:73-124`
**Severity:** INFO

EXCHCONN has defense-in-depth sanity checks (qty > 0, qty < 1000 hard limit at line 32-33), but any process that connects to `ws://localhost:8084` bypasses all OM risk checks. This is acceptable for a development/simulation platform running on localhost, but would be a critical gap in production. The hardcoded `MAX_ORDER_QTY = 1000` in EXCHCONN provides a coarse safety net.

**Recommendation:** For production deployment, add authentication or IP-based access control to the EXCHCONN WebSocket server.

### I-2: OM-to-POSMANAGER fill notifications can be lost, causing position drift
**File:** `om/order_manager.py:561-565`
**Severity:** INFO

When OM sends fill notifications to POSMANAGER (line 562), a failure is caught and logged (`logger.error`) but the fill is not retried or queued. If POSMANAGER is temporarily disconnected, fills are silently dropped. OM's internal `_positions` dict will diverge from POSMANAGER's position state. OM's simplified net-qty tracking (used for risk checks) would remain correct, but POSMANAGER's P&L calculations and the GUI's position display would be wrong.

The `WSClient.send()` method (line 127-130 of `ws_transport.py`) silently does nothing if `self._ws` is None, meaning if the connection drops and hasn't reconnected yet, the send is lost without raising an exception.

**Recommendation:** Implement a fill notification queue with retry, or add a position reconciliation mechanism (e.g., POSMANAGER queries OM for net positions periodically).

### I-3: Risk limit REST endpoint has no input validation on limit values
**File:** `gui/server.py:262-279`
**Severity:** INFO

The `/api/risk-limits` POST endpoint accepts arbitrary JSON and saves it directly via `risk_limits.save_limits()`. There is no validation that the values are positive numbers, that the expected keys exist, or that the structure matches what `check_order()` expects. A malformed POST (e.g., setting `max_order_qty` to a string or negative number) would be persisted, and `check_order()` would then call `float()` on potentially invalid data. The `float()` calls in `check_order` would raise `ValueError` for non-numeric strings, which is uncaught and would crash the risk check, effectively rejecting all orders (fail-closed behavior).

**Recommendation:** Validate limit values are positive numbers in the POST handler before saving.

---

## Final Counts

| Category | Count |
|----------|-------|
| PASSED   | 15    |
| WARNING  | 1     |
| INFO     | 3     |
| CRITICAL | 0     |
| ERROR    | 0     |

**Overall Assessment:** The risk control framework is solid. All four risk check types are applied on new orders. Amend and cancel paths are properly handled. The main risk concern (W-1) is conservative — it over-rejects rather than under-rejects. The informational items (EXCHCONN bypass, fill loss drift, unvalidated REST input) are design-level considerations appropriate for a development platform but should be addressed before production use.
