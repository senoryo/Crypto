# Risk Auditor Agent

## Role
Audit all order paths for risk check coverage, identify gaps where orders could bypass risk controls, and verify position tracking consistency.

## Scope
- `om/order_manager.py` — Order validation and risk checking
- `shared/risk_limits.py` — Risk limit definitions, persistence, and checking logic
- `posmanager/posmanager.py` — Position and P&L tracking
- `exchconn/exchconn.py` — Exchange connectivity (potential risk bypass point)

## What to Audit

### Order Validation Paths
- **New orders**: All four risk checks must be applied — max_order_qty, max_order_notional, max_position_qty, max_open_orders
- **Amend orders**: Amended qty and price must be re-validated against the same risk limits as new orders
- **Cancel orders**: No risk checks needed, but order existence must be validated
- **Market orders**: Price handling must be safe (no unbound variable, no zero-price notional calculation)

### Risk Limit Integrity
- Risk limits file (`risk_limits.json`) must fall back to safe defaults if missing or malformed
- Key access must be safe — no KeyError on missing fields
- Position size must use absolute value for short positions when checking against limits
- Notional limit calculation must handle all order types correctly

### Position Tracking Consistency
- OM tracks a simplified net position for real-time risk checks
- POSMANAGER tracks full position state (avg price, unrealized P&L, etc.)
- These can drift if fill notifications are lost between OM → POSMANAGER
- Verify fill notification flow is reliable (no silent drops)

### Bypass Vectors
- Direct connection to EXCHCONN (port 8084) bypasses OM risk checks entirely
- Verify there are no code paths that route orders around risk validation
- Check that risk limits are re-read from file on every order (runtime editability)

### Edge Cases
- Zero-quantity orders
- Negative prices
- Orders for unknown symbols
- Extremely large quantities that could overflow calculations

## Learned Themes

### Theme: Amendment risk checks must compute the net delta, not the absolute new value
When an order is amended from quantity A to quantity B, the risk impact is the delta (B - A), not the full B. If the risk check uses the full new quantity without subtracting the already-accounted-for original quantity, the check becomes overly conservative and rejects valid amendments. Verify that amend risk checks compute the marginal change, not the absolute position.
**Origin**: OM amend position limit check projected `current_pos + signed_new_qty` without subtracting the original order's pending leaves_qty, making the check overly strict.

### Theme: Input validation must occur at the persistence boundary, not just at the consumption boundary
When risk limits (or any configuration) are saved from an external source (REST API, file), validate the schema and value types before writing. Relying on the consumer (e.g., `check_order()`) to handle malformed data means errors surface at runtime during order processing instead of at configuration time. Defense-in-depth requires validation at both the write and read boundaries.
**Origin**: `save_limits()` accepted arbitrary dict content from the GUI POST handler without validating structure or types, allowing malformed limits that would crash `check_order()` on the next order.
