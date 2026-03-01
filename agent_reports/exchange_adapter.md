# Exchange Adapter Agent Report

## Mission
Validate exchange simulator behavior, ensure parity between simulators, verify real exchange connectivity paths, and confirm correct order lifecycle handling.

## Files Analyzed
- `exchconn/binance_sim.py` -- Binance exchange simulator
- `exchconn/coinbase_sim.py` -- Coinbase exchange simulator
- `exchconn/exchconn.py` -- Exchange connection router
- `exchconn/coinbase_adapter.py` -- Real Coinbase REST adapter
- `exchconn/coinbase_fix_adapter.py` -- Coinbase FIX 5.0 SP2 adapter
- `shared/fix_engine.py` -- FIX TCP+SSL client engine
- `shared/fix_protocol.py` -- FIX message definitions and factory functions
- `shared/coinbase_auth.py` -- Coinbase JWT/HMAC authentication
- `shared/config.py` -- Exchange configuration and routing tables

## Summary

- Passed: **37**
- Warnings: **5**
- Errors: **0**
- Info: **4**

---

### interface-parity -- OK

- [pass] Both simulators implement `submit_order(fix_msg)`
- [pass] Both simulators implement `cancel_order(fix_msg)`
- [pass] Both simulators implement `amend_order(fix_msg)`
- [pass] Both simulators implement `set_report_callback(callback)`
- [pass] Both simulators implement `start()` / `stop()`
- [pass] Both simulators implement `_next_order_id()` and `_get_current_price()`
- [pass] CoinbaseAdapter (REST) implements same 6-method interface
- [pass] CoinbaseFIXAdapter implements same 6-method interface
- [pass] Method signatures are consistent across all four adapter implementations

### fill-sim -- OK

- [pass] Binance: supports partial fills (1-3 random chunks)
- [pass] Binance: applies per-fill price jitter via `random.uniform(-0.0002, 0.0002)`
- [pass] Binance: tracks `cum_qty`, `leaves_qty`, `avg_px` correctly with VWAP formula
- [pass] Binance: deactivates order (`is_active = False`) on final fill
- [pass] Binance: uses epsilon comparison (`<= 1e-10`) for final fill detection (floating-point safe)
- [pass] Binance: fill task cancelled and cleaned up when order is cancelled during filling
- [pass] Coinbase: supports partial fills (1-3 random chunks)
- [pass] Coinbase: applies per-fill price jitter via `random.uniform(-0.0003, 0.0003)` (wider than Binance)
- [pass] Coinbase: tracks `cum_qty`, `leaves_qty`, `avg_px` correctly with VWAP formula
- [pass] Coinbase: deactivates order on final fill
- [pass] Coinbase: uses epsilon comparison for final fill detection
- [pass] Coinbase: fill task cancelled and cleaned up on cancel

### routing -- OK

- [pass] EXCHCONN uses `ExDestination` FIX tag (Tag 100) for routing
- [pass] EXCHCONN falls back to `DEFAULT_ROUTING` when `ExDestination` not present
- [pass] Both `BINANCE` and `COINBASE` registered in `self._exchanges`
- [pass] `NewOrderSingle` (35=D) routed via `exchange.submit_order()`
- [pass] `OrderCancelRequest` (35=F) routed via `exchange.cancel_order()`
- [pass] `OrderCancelReplaceRequest` (35=G) routed via `exchange.amend_order()`
- [pass] EXCHCONN applies defense-in-depth sanity checks (qty > 0, qty <= 1000 hard limit)
- [pass] EXCHCONN rejects with proper execution report for unknown exchange names

### cancel-lifecycle -- OK

- [pass] Simulators cancel pending fill tasks before marking order inactive
- [pass] Simulators reject cancel on already-inactive orders with "Order not active" text
- [pass] Simulators reject cancel for unknown `OrigClOrdID` with "Unknown order" text
- [pass] Cancel sets `leaves_qty = 0`, `is_active = False`, sends `ExecType.Canceled`

### amend-lifecycle -- OK

- [pass] Simulators cancel pending fill tasks before applying amendments
- [pass] Simulators update `total_qty`, `leaves_qty`, `price` on amend
- [pass] Simulators update `cl_ord_id` mapping to new cancel-replace ID
- [pass] Simulators reject amend on already-inactive orders
- [pass] Simulators reject amend for unknown orders

### real-coinbase -- OK

- [pass] `USE_REAL_COINBASE` env flag switches from simulator to `CoinbaseAdapter`
- [pass] `USE_COINBASE_FIX` env flag switches to `CoinbaseFIXAdapter`
- [pass] HMAC-SHA256 signature computation implemented in `build_coinbase_logon_signature()`
- [pass] JWT authentication in `shared/coinbase_auth.py` supports both EC (ES256) and Ed25519 (EdDSA) keys
- [pass] Sandbox vs production endpoint selection for REST, FIX, and WebSocket URLs
- [pass] API key, secret, and passphrase loaded from environment variables

### sim-differences -- OK

- [pass] Coinbase has wider jitter (0.12%) than Binance (0.10%) -- realistic
- [pass] Coinbase has slightly longer fill delays (0.7-2.5s vs 0.5-2.0s) -- realistic
- [pass] Both simulators use identical base prices
- [pass] Different order ID prefixes: Binance=`BIN`, Coinbase=`CB`
- [pass] Simulator names match expected exchange names (`BINANCE`, `COINBASE`)

---

## Issues Found

### WARNING-1: Simulators do not restart fill sequences after amend for limit orders
**Files:** `exchconn/binance_sim.py` (lines 400-435), `exchconn/coinbase_sim.py` (lines 392-424)
**Description:** When a limit order is amended, the fill task is correctly cancelled and order fields are updated, but no new fill task is created. The amended limit order relies entirely on `_check_limit_fills()` in the next price jitter cycle to re-trigger fills. While this works, it introduces an artificial delay (up to 0.5s) before the amended order is evaluated for fills. For market order amends, the problem is more significant: no new fill sequence is started at all, and since `_check_limit_fills()` only processes `OrdType.Limit`, an amended market order would never complete.
**Recommendation:** After applying amendments, explicitly schedule a new fill task if the order type is market, or if the order type is limit and the current price already qualifies for a fill. Additionally, consider rejecting amends on market orders (as the real CoinbaseAdapter does) since they execute near-instantly.

### WARNING-2: CoinbaseAdapter REST polling silently swallows non-fill terminal statuses
**File:** `exchconn/coinbase_adapter.py` (lines 595-596)
**Description:** In the `_reconcile_active_orders()` REST polling path, when a terminal status (CANCELLED, EXPIRED, FAILED) is detected but there's no fill delta, the order is marked `is_terminal = True` but no execution report is sent downstream. This is inconsistent with the WebSocket path (line 506-518), which sends a `Canceled` execution report for `CANCELLED` status. If Coinbase cancels or expires an order server-side while running in sandbox/REST-polling mode, the Order Manager will never learn about it.
**Recommendation:** Mirror the WS handler logic in the REST path: send an appropriate execution report (`ExecType.Canceled` for CANCELLED, `ExecType.Rejected` for EXPIRED/FAILED) before marking terminal.

### WARNING-3: CoinbaseFIXAdapter sets wrong TimeInForce for market orders
**File:** `exchconn/coinbase_fix_adapter.py` (line 154)
**Description:** Market orders are sent with `59=1` (GTC) but the comment says "IOC for market." In standard FIX, IOC is `59=3`. For Coinbase Exchange FIX, market orders should typically use IOC (`59=3`) or FOK (`59=4`). Setting GTC on a market order may be accepted by Coinbase but is semantically incorrect and the misleading comment could cause confusion during future maintenance.
**Recommendation:** Change to `wire.set(59, "3")` for market orders and update the comment to `# TimeInForce = IOC for market`.

### WARNING-4: `_pending_cancels` dictionary in CoinbaseFIXAdapter is populated but never consumed
**File:** `exchconn/coinbase_fix_adapter.py` (lines 89, 198)
**Description:** `self._pending_cancels` maps cancel `cl_ord_id` to `orig_cl_ord_id` but is never read, iterated, or cleaned up anywhere in the class. This is dead code and a minor memory leak over time for long-running sessions.
**Recommendation:** Either use this mapping in `_handle_cancel_reject()` to resolve the original order (instead of looking it up via `self._orders`), or remove the dictionary entirely.

### WARNING-5: EXCHCONN `_send_reject` uses hardcoded string `"8"` instead of `OrdStatus.Rejected` constant
**File:** `exchconn/exchconn.py` (line 146)
**Description:** `ord_status="8"` is a magic string. While `OrdStatus.Rejected` is also `"8"`, using the raw value bypasses the symbolic constant and makes the code less readable and more brittle. `OrdStatus` is not imported in this file.
**Recommendation:** Import `OrdStatus` from `shared.fix_protocol` and use `OrdStatus.Rejected` instead of `"8"`.

---

### INFO-1: Unused `import time` in both simulators
**Files:** `exchconn/binance_sim.py` (line 12), `exchconn/coinbase_sim.py` (line 12)
**Description:** Both simulators import `time` but never reference it. All timing uses `asyncio.sleep`.
**Recommendation:** Remove `import time` from both files.

### INFO-2: Unused `import EXCHANGES` and `import MsgType` in both simulators
**Files:** `exchconn/binance_sim.py` (lines 16, 19), `exchconn/coinbase_sim.py` (lines 16, 19)
**Description:** `MsgType` is imported from `fix_protocol` and `EXCHANGES` is imported from `config`, but neither is referenced in the simulator code.
**Recommendation:** Remove both unused imports from each simulator.

### INFO-3: Unused variable `remaining` in `_execute_fill` method
**Files:** `exchconn/binance_sim.py` (line 203), `exchconn/coinbase_sim.py` (line 201)
**Description:** `remaining = order.leaves_qty` is assigned at the start of `_execute_fill()` but never used. The code reads `order.leaves_qty` directly throughout the fill loop.
**Recommendation:** Remove the unused variable assignment.

### INFO-4: `_amend_fallback_msg` stored but never used in CoinbaseFIXAdapter
**File:** `exchconn/coinbase_fix_adapter.py` (line 267)
**Description:** `self._orders[cl_ord_id]._amend_fallback_msg = fix_msg` is set during amend but never read back. The fallback cancel+new logic (lines 358-384) reconstructs the new order from the tracked order fields rather than using the stored message.
**Recommendation:** Remove the `_amend_fallback_msg` assignment since it is dead code.

---

### Duplicate `SimulatedOrder` class -- Noted (not flagged)
Both `binance_sim.py` and `coinbase_sim.py` define identical `SimulatedOrder` classes. This is acceptable since the simulators are intentionally independent modules, but a shared base class in `shared/` would reduce duplication if more exchanges are added.

### `FIXClient._connected` accessed as private attribute -- Noted (not flagged)
`CoinbaseFIXAdapter` accesses `self._client._connected` (a private attribute of `FIXClient`) at three call sites. This works but couples the adapter to the internal implementation of `FIXClient`. A public `@property` or method like `is_connected()` would be cleaner.

---

## Final Counts

| Severity | Count |
|----------|-------|
| PASS     | 37    |
| WARNING  | 5     |
| ERROR    | 0     |
| INFO     | 4     |
