# Exchange Adapter Agent Report

## Date: 2026-03-01

## Summary

All three critical and high-severity findings from the prior report have been fixed in the current codebase. Both simulators now correctly restart fill sequences after amend with `(order, fill_price)` arguments, the CoinbaseAdapter WebSocket path now handles all terminal statuses (CANCELLED/EXPIRED/FAILED), and the CoinbaseFIXAdapter amend fallback guard uses `is not None` instead of the flawed `hasattr` check. Tests covering the amend-restart fix exist for both simulators. Several low-severity issues remain: unbounded order dictionary growth, dead variables, unused imports, and a stored-but-never-consumed attribute. One new medium-severity finding was identified: amended limit orders are not restarted and must wait for the next price tick cycle.

## Previously Fixed Issues (Verified)

### [FIXED] Amend restart in BinanceSimulator (was CRITICAL)

- **Prior Location**: `exchconn/binance_sim.py:443`
- **Status**: Fixed at lines 439-446. Code now correctly passes `(order, fill_price)` to `_execute_fill`.
- **Test Coverage**: `tests/exchconn/test_binance_sim.py:184-215` (`test_amend_market_order_schedules_fill_correctly`) verifies the fix.

### [FIXED] Amend restart in CoinbaseSimulator (was CRITICAL)

- **Prior Location**: `exchconn/coinbase_sim.py:432`
- **Status**: Fixed at lines 428-434. Code now correctly passes `(order, fill_price)` to `_execute_fill`.
- **Test Coverage**: `tests/exchconn/test_coinbase_sim.py:57-89` (`test_amend_market_order_schedules_fill_correctly`) verifies the fix.

### [FIXED] CoinbaseAdapter WS path terminal status handling (was HIGH)

- **Prior Location**: `exchconn/coinbase_adapter.py:503-518`
- **Status**: Fixed at lines 505-524. The `elif status in _TERMINAL_STATUSES and not tracked.is_terminal` block now correctly handles CANCELLED (ExecType.Canceled), and EXPIRED/FAILED (ExecType.Rejected) with appropriate execution reports. Parity with the REST polling path is confirmed.

### [FIXED] CoinbaseFIXAdapter `hasattr` guard (was MEDIUM)

- **Prior Location**: `exchconn/coinbase_fix_adapter.py:357`
- **Status**: Fixed at line 365. Now uses `if tracked and tracked._amend_fallback_orig is not None:` which correctly distinguishes amend-related cancel rejects from regular cancel rejects.

### [FIXED] CoinbaseFIXAdapter `_pending_cancels` dead data structure

- **Prior Location**: `exchconn/coinbase_fix_adapter.py`
- **Status**: Removed. Line 91 documents: `# _pending_cancels removed -- was populated but never consumed`.

## New Findings

### [MEDIUM] Amended limit orders rely on price-tick loop for fill restart, creating artificial delay

- **Category**: Cancel-Amend Lifecycle
- **Location**: `exchconn/binance_sim.py:439-446`, `exchconn/coinbase_sim.py:428-434`
- **Issue**: After an amend, the fill-restart code only schedules a new fill task for market orders (`if order.ord_type == OrdType.Market`). For limit orders, no fill task is scheduled. The amended limit order must wait until the next `_check_limit_fills()` iteration in `_price_jitter_loop()` (0.5s cycle) to be reconsidered. If the order was actively filling when the amend arrived (i.e., a fill task was running and was cancelled by the amend), the cancelled fill task is not replaced. Instead, `_check_limit_fills` will notice the order has no entry in `_fill_tasks` and (if the price condition is still met) will restart the fill sequence on the next tick. This creates a worst-case 0.5s artificial delay that would not exist with an explicit restart.
- **Impact**: Low-to-medium. 0.5s delay for limit order amend-restart is unlikely to cause functional issues in simulation mode but deviates from expected behavior where the amend should immediately restart execution if conditions are already met. For a simulator this is acceptable, but for consistency the pattern should match market orders.
- **Suggested Fix**: After the amend ack is sent, check if the amended limit order's price condition is already met and schedule a fill task immediately if so:
  ```python
  if order.is_active and order.leaves_qty > 0:
      if order.ord_type == OrdType.Market:
          fill_price = self._get_current_price(order.symbol)
          self._fill_tasks[order_id] = asyncio.create_task(
              self._execute_fill(order, fill_price)
          )
      elif order.ord_type == OrdType.Limit:
          current_price = self._get_current_price(order.symbol)
          should_fill = (
              (order.side == "1" and current_price <= order.price) or
              (order.side == "2" and current_price >= order.price)
          )
          if should_fill:
              self._fill_tasks[order_id] = asyncio.create_task(
                  self._execute_fill(order, fill_price=order.price)
              )
  ```

### [LOW] CoinbaseAdapter market buy path has duplicated code branches

- **Category**: Real Connectivity
- **Location**: `exchconn/coinbase_adapter.py:173-190`
- **Issue**: The `submit_order` method has identical code for market buy and market sell:
  ```python
  if cb_side == "BUY":
      response = await asyncio.to_thread(
          self._client.market_order,
          client_order_id=cl_ord_id,
          product_id=product_id,
          side=cb_side,
          base_size=str(qty),
      )
  else:
      response = await asyncio.to_thread(
          self._client.market_order,
          client_order_id=cl_ord_id,
          product_id=product_id,
          side=cb_side,
          base_size=str(qty),
      )
  ```
  The comment on line 175 hints the intent was to use `quote_size` for market buys (Coinbase requires quote denomination for market buys), but both branches use `base_size`. This means market buy orders may fail on Coinbase if the API enforces the distinction.
- **Impact**: Market buy orders on real Coinbase may fail or produce unexpected behavior. For the simulator path this is irrelevant, but for production Coinbase connectivity this could be a functional issue.
- **Suggested Fix**: Either collapse the branches into a single call (since they are identical), or correctly use `quote_size` for market buys as the comment suggests.

## Remaining Issues (from prior report, still present)

### [LOW] Both simulators accumulate orders indefinitely (unbounded memory growth)

- **Category**: Resilience
- **Location**: `exchconn/binance_sim.py:63-64`, `exchconn/coinbase_sim.py:63-64`, `exchconn/coinbase_adapter.py:94-95`
- **Issue**: `_orders` and `_cl_to_order` / `_cl_to_cb` dictionaries are append-only. Entries are never removed when orders reach terminal states. Same applies to `_orders` in CoinbaseFIXAdapter.
- **Impact**: Memory grows linearly with total orders over process lifetime. Negligible for normal usage.

### [LOW] Dead variable `remaining` in both simulators

- **Category**: Code Quality
- **Location**: `exchconn/binance_sim.py:203`, `exchconn/coinbase_sim.py:201`
- **Issue**: `remaining = order.leaves_qty` is assigned but never read. The fill loop uses `order.leaves_qty` directly.

### [LOW] Unused imports in both simulators

- **Category**: Code Quality
- **Location**: `exchconn/binance_sim.py:12,16,19`, `exchconn/coinbase_sim.py:12,16,19`
- **Issue**: `import time`, `MsgType` from fix_protocol, and `EXCHANGES` from config are imported but never used.

### [LOW] `_amend_fallback_msg` stored but never consumed

- **Category**: Code Quality
- **Location**: `exchconn/coinbase_fix_adapter.py:266`
- **Issue**: `_amend_fallback_msg = fix_msg` is assigned during amend but the fallback path reconstructs the order from tracked fields. Attribute is never read.

## Validated (No Issues Found)

- **Simulator Interface Parity**: Both BinanceSimulator and CoinbaseSimulator implement the identical 6-method interface (`submit_order`, `cancel_order`, `amend_order`, `set_report_callback`, `start`, `stop`). CoinbaseAdapter and CoinbaseFIXAdapter also conform. The `SimulatedOrder` class is identically defined in both simulators with matching field names and types.

- **Fill Simulation**: Partial fills (1-3 random chunks), per-fill price jitter (Binance 0.1%, Coinbase 0.12% -- confirmed wider spread), VWAP `avg_px` calculation, epsilon comparison (`<= 1e-10`) for final fill detection, `is_active` flag management, and fill task cleanup in `finally` blocks all verified correct.

- **Routing Logic**: `exchconn.py` routes via `ExDestination` tag, falls back to `DEFAULT_ROUTING` when absent, registers both exchanges, routes all three FIX message types (NewOrderSingle, CancelRequest, CancelReplaceRequest). Defense-in-depth quantity checks (qty > 0, qty <= 1,000,000) provide safety net. Comprehensive test coverage in `tests/exchconn/test_exchconn.py` (24 tests covering routing, dispatching, and sanity checks).

- **Cancel Lifecycle**: Both simulators correctly cancel active fill tasks (with `await` of cancelled task to prevent leaked coroutines), mark orders inactive, zero out `leaves_qty`, and send Canceled execution reports. Cancel of unknown/inactive orders sends appropriate Rejected reports with descriptive text.

- **Limit Order Fill Triggering**: `_check_limit_fills` correctly evaluates buy (market <= limit) and sell (market >= limit) conditions, skips orders already being filled, and creates fill tasks at the limit price.

- **Real Coinbase REST Connectivity**: CoinbaseAdapter uses `coinbase-advanced-py` SDK with JWT-patched authentication supporting both EC/PEM (ES256) and Ed25519 (EdDSA) keys. Sandbox/production mode selection, WebSocket fill monitoring with reconnection and backoff, REST polling fallback for sandbox, and fill delta computation all verified correct.

- **FIX Engine**: TCP+SSL client with HMAC-SHA256 Logon, heartbeat/TestRequest, sequence numbers, reconnection with exponential backoff (2s-60s), and atomic send via `asyncio.Lock`. Message framing via regex boundary detection, session-level message handling (Logon, Logout, SequenceReset, Reject, TestRequest) all correct.

- **Execution Report Queuing**: `exchconn.py` queues reports via bounded `deque(maxlen=1000)` when OM is disconnected, replays on reconnect. Prevents lost reports during transient disconnections.

- **CoinbaseFIXAdapter Amend Fallback**: Cancel+new fallback for rejected amends correctly generates a cancel request with a unique ID, waits 0.5s, then submits a new order. The new tracked order correctly stores amend context for fallback detection.

- **Test Coverage**: 24 tests in `tests/exchconn/test_exchconn.py`, 11 tests in `tests/exchconn/test_binance_sim.py`, 6 tests in `tests/exchconn/test_coinbase_sim.py` covering order lifecycle, routing, dispatching, sanity checks, and the amend-restart fix. No tests exist for CoinbaseAdapter or CoinbaseFIXAdapter (these require external service mocking).

## Counts

- Previously critical/high issues now fixed: 4
- New medium findings: 1
- New low findings: 1
- Remaining low findings from prior report: 4
- **Total open issues: 6 (1 medium, 5 low)**
