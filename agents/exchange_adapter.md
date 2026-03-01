# Exchange Adapter Agent

## Role
Validate exchange simulator behavior, ensure parity between simulators, verify real exchange connectivity paths, and confirm correct order lifecycle handling.

## Scope
- `exchconn/binance_sim.py` — Binance exchange simulator
- `exchconn/coinbase_sim.py` — Coinbase exchange simulator
- `exchconn/exchconn.py` — Exchange connection router
- `shared/fix_engine.py` — FIX engine used for real exchange connectivity
- `shared/config.py` — Exchange configuration, routing tables, base prices

## What to Validate

### Simulator Interface Parity
- Both simulators must implement the same interface: `submit_order`, `cancel_order`, `amend_order`, `set_report_callback`, `start`, `stop`
- Method signatures and return types should be consistent
- Both must support the same order types and message formats

### Fill Simulation
- Partial fill support — orders fill in multiple chunks, not all-at-once
- Price variation per fill (jitter) — realistic price movement during fill sequence
- Order state tracking: `cum_qty`, `leaves_qty`, `avg_px` updated correctly after each fill
- `is_active` flag set correctly — false after final fill or cancel
- Epsilon comparison for detecting final fill (floating-point safe)
- Fill task cancellation when an order is cancelled or amended during filling

### Routing Logic
- `ExDestination` FIX tag routes to correct simulator
- `DEFAULT_ROUTING` fallback from config works for symbols without explicit routing
- Both exchanges are registered in the routing table
- All message types routed correctly: NewOrderSingle, CancelRequest, CancelReplaceRequest

### Cancel/Amend Lifecycle
- Cancel: active fill task is cancelled, order marked inactive, CancelAck execution report sent
- Amend: active fill task is cancelled, order fields updated, new fill sequence started, amend ack sent
- Cancel of already-filled order returns appropriate reject
- Amend of already-filled order returns appropriate reject

### Real Coinbase Connectivity
- `USE_REAL_COINBASE` environment flag switches from simulator to live API
- HMAC-SHA256 signature computation for API authentication
- API key, secret, and passphrase configuration from environment
- Sandbox vs production endpoint selection

### Resilience
- Simulator handles rapid cancel-then-resubmit sequences correctly
- No resource leaks from cancelled fill tasks
- Order ID generation is unique and atomic

## Learned Themes

### Theme: Order lifecycle state machines must produce an outgoing message for every terminal state transition, not just fills
When an order reaches a terminal state (cancelled, expired, failed, rejected) through any path (REST polling, WebSocket event, timeout), the adapter must emit an execution report downstream. If only fill-related transitions emit reports, orders that terminate without fills become invisible to the order management layer.
**Origin**: CoinbaseAdapter REST polling detected terminal statuses (CANCELLED, EXPIRED, FAILED) but only sent exec reports for fill deltas, silently swallowing non-fill terminal transitions.

### Theme: After an amend modifies order state, any interrupted execution sequence must be explicitly restarted
When an amend cancels an in-progress fill task and updates order fields, the new state needs a fresh execution trigger. If the code relies on a periodic background check to pick up the amended order, there is an artificial delay; if the background check does not cover the order type (e.g., market orders), the amended order will never complete.
**Origin**: Simulators cancelled fill tasks on amend but did not schedule new fill tasks, leaving amended market orders permanently incomplete.

### Theme: Dead data structures (populated but never read) indicate incomplete feature implementation or abandoned refactoring
When a dictionary or collection is populated during a flow but never read, iterated, or cleaned up, it suggests either an incomplete feature (the consumption code was never written) or an abandoned refactoring. Flag these as both code quality issues and potential memory leaks.
**Origin**: `_pending_cancels` in CoinbaseFIXAdapter was populated on every cancel but never consumed anywhere in the class.
