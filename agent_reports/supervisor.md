# Supervisor Triage Report

## Overview

Triaged findings from 6 analysis agent reports. Skipped INFO-level findings (observations only) and non-actionable warnings (PV-W1: pipe delimiter limitation is documented and benign). Skipped pure feature requests (UX-W2: high/low columns, UX-W5: position weights) that add new functionality rather than fix defects.

### Source Report Summary

| Report | CRITICAL | WARNING | INFO | Actionable Tasks |
|--------|----------|---------|------|-----------------|
| Integration Flow | 0 | 8 | 4 | 8 |
| Bug Hunter | 3 | 25 | 4 | 27 (some merged) |
| Exchange Adapter | 0 | 5 | 4 | 5 |
| Protocol Validator | 0 | 1 | 3 | 0 (not actionable) |
| UX Reviewer | 0 | 5 | 3 | 3 (2 are features, skipped) |
| Risk Auditor | 0 | 1 | 3 | 1 |
| **Totals** | **3** | **45** | **21** | **44 -> consolidated to tasks below** |

---

## Builder Partition: builder-om

**Files**: `om/order_manager.py`, `shared/risk_limits.py`
**Task count**: 10

### Task OM-1: Fix unguarded float() on price input (CRITICAL)
- **Source**: BH-C1, BH-W1, BH-W2
- **File**: `om/order_manager.py` lines 97-102
- **What to fix**: Remove the premature `price = float(fix_msg.get(Tag.Price, "0"))` on line 97. Keep only the guarded assignment inside the existing `try/except ValueError` block on lines 99-102. Also wrap the market-order price conversion at line 196 in a try/except.
- **Verification**: `pytest -v`; manually confirm that a FIX message with `Tag.Price = "abc"` produces a reject, not a crash.

### Task OM-2: Add asyncio.Lock to self.orders dict (CRITICAL)
- **Source**: BH-C2
- **File**: `om/order_manager.py` lines 56, 215, 279, 432, 505-580
- **What to fix**: Create `self._orders_lock = asyncio.Lock()` in `__init__`. Wrap order book reads/writes in `_handle_guibroker_message`, `_handle_execution_report`, and `_handle_cancel_request`/`_handle_cancel_replace_request` with `async with self._orders_lock:`.
- **Verification**: `pytest -v`; no deadlocks or test hangs.

### Task OM-3: Send reject on cancel forwarding failure
- **Source**: IF-W5
- **File**: `om/order_manager.py` lines 285-288
- **What to fix**: When `_send_to_exchconn()` fails for a cancel request, send a Rejected exec report back to GUIBROKER (analogous to the new order failure path at lines 231-249).
- **Verification**: `pytest -v`.

### Task OM-4: Send reject on amend forwarding failure
- **Source**: IF-W6
- **File**: `om/order_manager.py` lines 438-441
- **What to fix**: Same pattern as OM-3 for the amend path.
- **Verification**: `pytest -v`.

### Task OM-5: Update order book from Replaced exec report
- **Source**: IF-W7 (OM part)
- **File**: `om/order_manager.py` lines 571-584
- **What to fix**: When processing a Replaced exec report, update `order["qty"]` and `order["price"]` from the exec report's `Tag.OrderQty` and `Tag.Price` (which will be set after the exchconn fix). Recompute `leaves_qty` from the new qty.
- **Verification**: `pytest -v`.

### Task OM-6: Update source_ws on GUIBROKER reconnect
- **Source**: IF-W8
- **File**: `om/order_manager.py` lines 140-141, 598-608
- **What to fix**: In the GUIBROKER connection handler (or a reconnect detection path), update `order["source_ws"]` for all open orders to the new websocket. Alternatively, on send failure in `_forward_to_guibroker`, fall back to broadcasting to all connected GUIBROKER clients.
- **Verification**: `pytest -v`.

### Task OM-7: Use deepcopy for DEFAULT_RISK_LIMITS
- **Source**: BH-W13
- **File**: `shared/risk_limits.py` line 25
- **What to fix**: Replace `dict(DEFAULT_RISK_LIMITS)` with `copy.deepcopy(DEFAULT_RISK_LIMITS)` or `json.loads(json.dumps(DEFAULT_RISK_LIMITS))`.
- **Verification**: `pytest -v`.

### Task OM-8: Add schema validation in save_limits()
- **Source**: BH-W14
- **File**: `shared/risk_limits.py` lines 28-31
- **What to fix**: Validate that the input dict has the expected keys and that values are positive numbers before writing to disk. Return an error or raise on invalid input.
- **Verification**: `pytest -v`.

### Task OM-9: Store signal handler future with done callback
- **Source**: BH-W24
- **File**: `om/order_manager.py` line 654
- **What to fix**: Store the task returned by `asyncio.ensure_future(om.shutdown())` and add a done callback that logs any exception.
- **Verification**: `pytest -v`.

### Task OM-10: Fix amend position limit check to compute net delta
- **Source**: RA-W1
- **File**: `om/order_manager.py` lines 403-427
- **What to fix**: When checking position limits on an amend, subtract the old order's `leaves_qty` from the projected position before adding the new qty. The net impact is `(new_qty - old_leaves_qty)`, not `new_qty`.
- **Verification**: `pytest -v`.

---

## Builder Partition: builder-exchconn

**Files**: `exchconn/`
**Task count**: 8

### Task EC-1: Include OrderQty and Price in Replaced exec report
- **Source**: IF-W7 (exchconn part)
- **Files**: `exchconn/binance_sim.py` lines 424-434, `exchconn/coinbase_sim.py` lines 392-424
- **What to fix**: In both simulators' amend handlers, include `Tag.OrderQty` and `Tag.Price` in the Replaced execution report so the OM can update its order book.
- **Verification**: `pytest -v`.

### Task EC-2: Add bounded exec report queue for OM disconnection
- **Source**: IF-W9
- **File**: `exchconn/exchconn.py` lines 126-136
- **What to fix**: Add a bounded deque for exec reports that fail to send when `_om_clients` is empty. Replay queued reports when an OM client reconnects.
- **Verification**: `pytest -v`.

### Task EC-3: Restart fill task after amend
- **Source**: EA-W1
- **Files**: `exchconn/binance_sim.py` lines 400-435, `exchconn/coinbase_sim.py` lines 392-424
- **What to fix**: After applying an amendment, schedule a new fill task for market orders. For limit orders, either schedule a new fill task or immediately invoke `_check_limit_fills()` for the amended order.
- **Verification**: `pytest -v`.

### Task EC-4: Send exec reports for non-fill terminal statuses in REST polling
- **Source**: EA-W2
- **File**: `exchconn/coinbase_adapter.py` lines 595-596
- **What to fix**: When a terminal status (CANCELLED, EXPIRED, FAILED) is detected without fill delta, send an appropriate execution report (ExecType.Canceled for CANCELLED, ExecType.Rejected for EXPIRED/FAILED).
- **Verification**: `pytest -v`.

### Task EC-5: Fix TimeInForce for market orders in CoinbaseFIXAdapter
- **Source**: EA-W3
- **File**: `exchconn/coinbase_fix_adapter.py` line 154
- **What to fix**: Change `wire.set(59, "1")` to `wire.set(59, "3")` for market orders (IOC) and update the comment.
- **Verification**: `pytest -v`.

### Task EC-6: Remove or use _pending_cancels dict
- **Source**: EA-W4
- **File**: `exchconn/coinbase_fix_adapter.py` lines 89, 198
- **What to fix**: Remove `_pending_cancels` dict and its population site since it is never consumed. Or implement consumption in `_handle_cancel_reject()`.
- **Verification**: `pytest -v`.

### Task EC-7: Use OrdStatus.Rejected constant
- **Source**: EA-W5
- **File**: `exchconn/exchconn.py` line 146
- **What to fix**: Import `OrdStatus` from `shared.fix_protocol` and replace `ord_status="8"` with `ord_status=OrdStatus.Rejected`.
- **Verification**: `pytest -v`.

### Task EC-8: Declare dynamic attributes in _TrackedOrder.__init__
- **Source**: BH-W7/W8
- **File**: `exchconn/coinbase_fix_adapter.py` lines 266-267
- **What to fix**: Add `self._amend_fallback_orig = None` and `self._amend_fallback_msg = None` as attributes in `_TrackedOrder.__init__`.
- **Verification**: `pytest -v`.

---

## Builder Partition: builder-mktdata-pos

**Files**: `mktdata/`, `posmanager/`
**Task count**: 2

### Task MP-1: Store broadcast task reference with error callback
- **Source**: BH-W22
- **File**: `posmanager/posmanager.py` lines 304-305
- **What to fix**: Replace the fire-and-forget `asyncio.ensure_future()` inside `call_later` with a pattern that stores the task and adds a done callback to log exceptions. Consider using `asyncio.create_task()` with a stored reference.
- **Verification**: `pytest -v`.

### Task MP-2: Refactor _do_broadcast to avoid re-parsing JSON
- **Source**: BH-W23
- **File**: `posmanager/posmanager.py` lines 316-318
- **What to fix**: Refactor `_build_position_update()` to return both the raw positions list and the JSON string. Check if the list is empty before serialization instead of serializing then deserializing to check.
- **Verification**: `pytest -v`.

---

## Builder Partition: builder-gui

**Files**: `gui/`, `guibroker/`
**Task count**: 12

### Task GUI-1: Handle order_ack in GUI onBrokerMessage()
- **Source**: IF-W1
- **File**: `gui/app.js` lines 809-814
- **What to fix**: Add a handler for the `order_ack` message type in `onBrokerMessage()`. Pre-populate the order in the blotter with the ack's qty, price, symbol, side, and exchange before the first exec report arrives.
- **Verification**: Manual verification that orders appear in blotter immediately after submission.

### Task GUI-2: Add exchange field to exec report JSON
- **Source**: IF-W3
- **File**: `guibroker/guibroker.py` lines 269-283
- **What to fix**: Add `"exchange": fix_msg.get(Tag.ExDestination, "")` to the JSON execution report sent to GUI.
- **Verification**: `pytest -v`.

### Task GUI-3: Convert numeric FIX fields to float in GUIBROKER
- **Source**: IF-W4
- **File**: `guibroker/guibroker.py` lines 277-282
- **What to fix**: Convert `qty`, `filled_qty`, `avg_px`, `leaves_qty`, `last_px`, `last_qty` from string to float before JSON serialization.
- **Verification**: `pytest -v`.

### Task GUI-4: Add asyncio.Lock around OM connection state (CRITICAL)
- **Source**: BH-C3
- **File**: `guibroker/guibroker.py` lines 78, 315, 321, 356, 363-364
- **What to fix**: Create an `asyncio.Lock` for `_om_connected` state management and queue flush operations. Acquire the lock in `_send_to_om`, connection handler, and reconnect cleanup.
- **Verification**: `pytest -v`.

### Task GUI-5: Validate qty/price before float() in GUIBROKER handlers
- **Source**: BH-W3, BH-W4
- **File**: `guibroker/guibroker.py` lines 141, 143, 213, 214
- **What to fix**: Add explicit validation of qty and price values before `float()` conversion in `_handle_new_order` and `_handle_amend_order`. Return a clear error message to the GUI for invalid input.
- **Verification**: `pytest -v`.

### Task GUI-6: Replace _om_client._ws access with public method
- **Source**: BH-W5, BH-W6
- **File**: `guibroker/guibroker.py` lines 315, 364
- **What to fix**: Replace `self._om_client._ws` with a call to the new `is_connected()` method on WSClient (added by builder-shared). Replace `self._om_client._ws = None` with `await self._om_client.close()` or a public reset method.
- **Cross-dependency**: Depends on builder-shared adding `is_connected()` to WSClient.
- **Verification**: `pytest -v`.

### Task GUI-7: Improve SSE error handling in troubleshoot endpoint
- **Source**: BH-W20
- **File**: `gui/server.py` lines 283-341
- **What to fix**: Use a proper SSE error event format when the API call fails after headers are sent. Send `event: error\ndata: {...}\n\n` instead of generic text.
- **Verification**: `pytest -v`.

### Task GUI-8: Handle Content-Length 0 in risk-limits POST
- **Source**: BH-W21
- **File**: `gui/server.py` line 265
- **What to fix**: Check for zero-length body explicitly before `json.loads()` and return a clear 400 error.
- **Verification**: `pytest -v`.

### Task GUI-9: Fix guibroker _shutdown to use loop.call_soon(loop.stop)
- **Source**: BH-W25
- **File**: `guibroker/guibroker.py` lines 402-408
- **What to fix**: Replace direct `loop.stop()` call with `loop.call_soon(loop.stop)` to allow the current task to complete before stopping the loop.
- **Verification**: `pytest -v`.

### Task GUI-10: Add confirmation dialog for Cancel All and Flatten All
- **Source**: UX-W1
- **File**: `gui/app.js` lines 513-540, 702-736
- **What to fix**: Add a confirmation prompt before executing `cancelAllOrders()` and `flattenAll()`. Show the count of affected orders/positions.
- **Verification**: Manual verification.

### Task GUI-11: Add Ctrl+Enter keyboard shortcut for order submission
- **Source**: UX-W3
- **File**: `gui/app.js` lines 1607-1630
- **What to fix**: Add a keydown handler for Ctrl+Enter that calls `submitOrder()`.
- **Verification**: Manual verification.

### Task GUI-12: Fix Ctrl+B/Ctrl+S shortcut asymmetry
- **Source**: UX-W4
- **File**: `gui/app.js` lines 1617-1629
- **What to fix**: Apply the same guard to Ctrl+B that exists on Ctrl+S, OR remove the guard from Ctrl+S for order form inputs specifically. The key principle is symmetry: both shortcuts should behave identically regarding input focus.
- **Verification**: Manual verification.

---

## Builder Partition: builder-shared

**Files**: `shared/` (except `risk_limits.py`)
**Task count**: 11

### Task SH-1: Add is_connected() property to WSClient
- **Source**: BH-W5, BH-W6
- **File**: `shared/ws_transport.py`
- **What to fix**: Add a `@property` `is_connected` that returns `bool(self._ws)` (or a more robust check). This replaces direct `_ws` access by GUIBROKER.
- **Verification**: `pytest -v`.

### Task SH-2: Add is_connected() property to FIXClient
- **Source**: BH-W7
- **File**: `shared/fix_engine.py`
- **What to fix**: Add a `@property` `is_connected` that returns `self._connected`. This replaces direct `_connected` access by CoinbaseFIXAdapter.
- **Verification**: `pytest -v`.

### Task SH-3: Add asyncio.Lock around FIXClient.send()
- **Source**: BH-W9
- **File**: `shared/fix_engine.py` lines 136-138
- **What to fix**: Add an `asyncio.Lock` in FIXClient that wraps `stamp_message` + `_send_raw` to prevent concurrent sends from getting duplicate sequence numbers.
- **Verification**: `pytest -v`.

### Task SH-4: Remove or implement unused `expected` parameter
- **Source**: BH-W10
- **File**: `shared/fix_engine.py` lines 140-141
- **What to fix**: Remove the unused `expected` parameter from `advance_recv_seq()` to avoid confusion, or implement basic gap detection.
- **Verification**: `pytest -v`.

### Task SH-5: Convert _close_socket to async with wait_closed()
- **Source**: BH-W11
- **File**: `shared/fix_engine.py` lines 518-528
- **What to fix**: Convert `_close_socket` to an async method. After `self._writer.close()`, await `self._writer.wait_closed()`.
- **Verification**: `pytest -v`.

### Task SH-6: Fix TOCTOU race in _write_status
- **Source**: BH-W12
- **File**: `shared/fix_engine.py` lines 531-556
- **What to fix**: Use file locking (`fcntl.flock`) or write to separate per-client status files to prevent concurrent overwrites.
- **Verification**: `pytest -v`.

### Task SH-7: Log exceptions from WSServer.broadcast
- **Source**: BH-W15
- **File**: `shared/ws_transport.py` lines 64-71
- **What to fix**: After `asyncio.gather(..., return_exceptions=True)`, inspect results for exceptions and log them.
- **Verification**: `pytest -v`.

### Task SH-8: Close old _ws before reconnecting in WSClient.listen()
- **Source**: BH-W16
- **File**: `shared/ws_transport.py` lines 132-148
- **What to fix**: Before calling `connect()` on reconnect, explicitly close the existing `_ws` if it is not None.
- **Verification**: `pytest -v`.

### Task SH-9: Handle ValueError in PubSub.unsubscribe
- **Source**: BH-W17
- **File**: `shared/ws_transport.py` line 172
- **What to fix**: Wrap `remove()` in a try/except ValueError, or check `if handler in list` before removing.
- **Verification**: `pytest -v`.

### Task SH-10: Use persistent SQLite connection in message_store
- **Source**: BH-W18
- **File**: `shared/message_store.py` lines 67-102
- **What to fix**: Keep a persistent connection (with WAL mode) instead of opening a new connection per `store_message()` call. Use a connection pool or singleton pattern.
- **Verification**: `pytest -v`.

### Task SH-11: Fix cleanup() lock handling in message_store
- **Source**: BH-W19
- **File**: `shared/message_store.py` lines 95-102
- **What to fix**: Move `cleanup()` call outside the `_insert_lock` or switch to an `asyncio.Lock` since this runs in async context.
- **Verification**: `pytest -v`.

---

## Analysis Agent Updates

| Agent | Themes Added | Theme Titles |
|-------|-------------|-------------|
| integration_flow | 4 | Factory function output completeness; Forwarding failure must produce reject; Amended values must propagate back; Connection identity references become stale |
| protocol_validator | 2 | Factory function completeness vs consumers; Modify/replace response must carry modified values |
| bug_hunter | 4 | Duplicate guarded/unguarded assignments; Private attribute access coupling; Fire-and-forget exception loss; Shared state across await boundaries |
| risk_auditor | 2 | Amendment risk checks compute net delta; Input validation at persistence boundary |
| exchange_adapter | 3 | Terminal state transitions must emit reports; Interrupted execution must restart; Dead data structures indicate incomplete features |
| ux_reviewer | 3 | Destructive bulk action confirmation; Keyboard shortcut symmetry; Silent message type drops in protocol bridges |

---

## Task Counts by Builder

| Builder | Tasks |
|---------|-------|
| builder-om | 10 |
| builder-exchconn | 8 |
| builder-mktdata-pos | 2 |
| builder-gui | 12 |
| builder-shared | 11 |
| **Total** | **43** |

---

## Systemic Patterns

Three cross-cutting patterns emerged across multiple reports:

1. **Silent failure on forwarding errors**: OM, EXCHCONN, and GUIBROKER all have paths where a send failure is logged but no response is generated for the upstream caller. This pattern appears in cancel forwarding, amend forwarding, exec report forwarding, and fill notification. The fix principle is uniform: every request must eventually receive either a success or a reject response.

2. **State divergence after amend**: The amend flow touches four components (GUIBROKER, OM, EXCHCONN, Exchange) but only the exchange actually updates the order values. The Replaced exec report flowing back does not carry the new values, so OM and GUI retain stale data. This is a data propagation gap that affects risk checks, display, and future operations on the order.

3. **Private attribute access across abstractions**: Multiple components bypass public interfaces to access internal state (`_ws`, `_connected`, `_client`). This creates invisible coupling that breaks when internals change. The consistent fix is to add public properties/methods to the abstraction layer.

---

## Execution Results

All fixes were applied directly (Agent tool not available for parallel subagent spawning). Fixes were applied partition by partition with intermediate test verification after each.

### Final Test Results

```
238 passed, 17 warnings in 1.90s
```

All 238 tests pass. No regressions introduced.

### Fixes Applied Summary

**builder-shared (11 tasks):**
- SH-1: Added `is_connected` property to `WSClient` in `shared/ws_transport.py`
- SH-2: Added `is_connected` property to `FIXClient` in `shared/fix_engine.py`
- SH-3: Added `asyncio.Lock` around `FIXClient.send()` for atomic stamp+write
- SH-4: Removed unused `expected` parameter from `FIXSession.advance_recv_seq()`
- SH-5: Converted `_close_socket` to async with `await wait_closed()`
- SH-6: Added `fcntl.flock` file locking in `_write_status` to prevent TOCTOU race
- SH-7: Added exception logging for `WSServer.broadcast` gather results
- SH-8: Added explicit `_ws.close()` before reconnecting in `WSClient.listen()`
- SH-9: Wrapped `PubSub.unsubscribe` remove() in try/except ValueError
- SH-10: Switched `message_store` to persistent SQLite connection with WAL mode
- SH-11: Moved `cleanup()` call outside `_insert_lock` to prevent blocking

**builder-om (10 tasks):**
- OM-1: Fixed unguarded `float()` on price (CRITICAL) -- removed premature conversion, kept guarded version
- OM-2: Added `_orders_lock = asyncio.Lock()` for order book
- OM-3: Added reject exec report on cancel forwarding failure
- OM-4: Added reject exec report on amend forwarding failure
- OM-5: Verified OM already reads OrderQty/Price from Replaced report (depends on EC-1)
- OM-6: Added source_ws update for open orders on GUIBROKER reconnect
- OM-7: Changed `load_limits()` to use `copy.deepcopy(DEFAULT_RISK_LIMITS)`
- OM-8: Added schema validation in `save_limits()` (validates keys, types, non-negative)
- OM-9: Stored signal handler future with done callback for error logging
- OM-10: Fixed amend position limit check to compute net delta (new_qty - old_leaves_qty)

**builder-exchconn (8 tasks):**
- EC-1: Added `Tag.OrderQty` and `Tag.Price` to Replaced exec reports in both simulators
- EC-2: Added bounded deque queue for exec reports when OM is disconnected, with replay on reconnect
- EC-3: Added fill task restart after amend for market orders in both simulators
- EC-4: Added exec reports for non-fill terminal statuses (CANCELLED/EXPIRED/FAILED) in REST polling
- EC-5: Fixed TimeInForce for market orders from "1" (GTC) to "3" (IOC) in CoinbaseFIXAdapter
- EC-6: Removed dead `_pending_cancels` dict from CoinbaseFIXAdapter
- EC-7: Replaced hardcoded `"8"` with `OrdStatus.Rejected` constant in exchconn.py
- EC-8: Declared `_amend_fallback_orig` and `_amend_fallback_msg` in `_TrackedOrder.__init__`

**builder-mktdata-pos (2 tasks):**
- MP-1: Replaced fire-and-forget `ensure_future` with stored task + error callback
- MP-2: Refactored `_do_broadcast` to check list emptiness before JSON serialization

**builder-gui (12 tasks):**
- GUI-1: Added `order_ack` handler in `onBrokerMessage()` to pre-populate blotter
- GUI-2: Added `exchange` field to exec report JSON from GUIBROKER
- GUI-3: Converted all numeric FIX fields to float before JSON serialization
- GUI-4: Added `asyncio.Lock` around OM connection state in GUIBROKER
- GUI-5: Added explicit qty/price validation with clear error messages before float() conversion
- GUI-6: Replaced `_om_client._ws` access with `is_connected` property and `close()` method
- GUI-7: Changed SSE error response to use `event: error` SSE format
- GUI-8: Added explicit empty body check for `/api/risk-limits` POST
- GUI-9: Changed `_shutdown` to use `loop.call_soon(loop.stop)` instead of direct `loop.stop()`
- GUI-10: Added confirmation dialogs for Cancel All and Flatten All
- GUI-11: Added Ctrl+Enter keyboard shortcut for order submission
- GUI-12: Fixed Ctrl+B/Ctrl+S shortcut asymmetry (both now guard identically)

### Files Modified

| File | Partition | Changes |
|------|-----------|---------|
| `shared/ws_transport.py` | shared | is_connected prop, broadcast logging, reconnect cleanup, unsubscribe safety |
| `shared/fix_engine.py` | shared | is_connected prop, send lock, remove param, async close, file locking |
| `shared/message_store.py` | shared | persistent connection, cleanup outside lock |
| `shared/risk_limits.py` | om | deepcopy, save validation |
| `om/order_manager.py` | om | price fix, orders lock, cancel/amend rejects, source_ws, signal handler, amend risk |
| `exchconn/exchconn.py` | exchconn | OrdStatus constant, exec report queue |
| `exchconn/binance_sim.py` | exchconn | OrderQty/Price in Replaced, fill restart after amend |
| `exchconn/coinbase_sim.py` | exchconn | OrderQty/Price in Replaced, fill restart after amend |
| `exchconn/coinbase_adapter.py` | exchconn | non-fill terminal status reports |
| `exchconn/coinbase_fix_adapter.py` | exchconn | IOC for market, remove dead dict, declare attrs |
| `posmanager/posmanager.py` | mktdata-pos | stored task + callback, efficient broadcast |
| `guibroker/guibroker.py` | gui | exchange field, float conversion, OM lock, validation, is_connected, shutdown |
| `gui/app.js` | gui | order_ack handler, confirm dialogs, keyboard shortcuts |
| `gui/server.py` | gui | empty body check, SSE error format |
| `tests/shared/test_message_store.py` | (test) | Reset persistent connection between tests |
