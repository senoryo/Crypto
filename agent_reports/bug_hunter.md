# Bug Hunter Agent Report

## Mission
Static analysis for async pitfalls, unbound variables, race conditions, silent exceptions, type coercion issues, logic errors, and code defects across all Python source files (excluding tests/, agents/, .git/, __pycache__/, venv/).

## Files Scanned (24 files)

| # | File |
|---|------|
| 1 | `shared/config.py` |
| 2 | `shared/fix_protocol.py` |
| 3 | `shared/ws_transport.py` |
| 4 | `shared/risk_limits.py` |
| 5 | `shared/logging_config.py` |
| 6 | `shared/coinbase_auth.py` |
| 7 | `shared/message_store.py` |
| 8 | `shared/fix_engine.py` |
| 9 | `om/order_manager.py` |
| 10 | `guibroker/guibroker.py` |
| 11 | `exchconn/exchconn.py` |
| 12 | `exchconn/binance_sim.py` |
| 13 | `exchconn/coinbase_sim.py` |
| 14 | `exchconn/coinbase_adapter.py` |
| 15 | `exchconn/coinbase_fix_adapter.py` |
| 16 | `mktdata/mktdata.py` |
| 17 | `mktdata/binance_feed.py` |
| 18 | `mktdata/coinbase_feed.py` |
| 19 | `mktdata/coinbase_live_feed.py` |
| 20 | `mktdata/coinbase_fix_feed.py` |
| 21 | `posmanager/posmanager.py` |
| 22 | `gui/server.py` |
| 23 | `run_all.py` |
| 24 | `restart.py` |

## Summary

- **CRITICAL**: 3
- **WARNING**: 25
- **INFO**: 4
- **Total Issues**: 32

---

## CRITICAL Issues

### C-1: Unsafe `float()` conversion on unvalidated price input before limit-order guard (OM)

- **File**: `om/order_manager.py`, line 97
- **Description**: In `_validate_order()`, the code calls `price = float(fix_msg.get(Tag.Price, "0"))` on line 97 *before* the `try/except ValueError` guard on lines 99-102. If `Tag.Price` contains a non-numeric string (e.g. "abc"), the `float()` on line 97 will raise an unhandled `ValueError`, crashing the entire message handler. The `try/except` block on lines 99-102 that was intended to catch this only covers the *second* assignment to `price`, which is dead code since `price` is already set.
- **Recommendation**: Remove the premature `float()` on line 97, or move it inside the existing `try/except` on line 99. The code currently has two assignments to `price`: one unguarded on line 97 and one guarded on line 100 -- the guarded one is dead code when `ord_type == Limit` because line 97 already crashed.

### C-2: Race condition -- `self.orders` dict shared across concurrent async tasks without lock (OM)

- **File**: `om/order_manager.py`, lines 56, 215, 279, 432, 505-510, 521-580
- **Description**: `self.orders` is a plain `dict` accessed from both `_handle_guibroker_message` (server handler, one per connected client) and `_handle_exchconn_message` (client listener) concurrently. While Python's GIL prevents data corruption, the interleaving of `await` points means one coroutine can read stale order state while another is mid-update. For example, `_handle_execution_report` reads `self.orders.get(cl_ord_id)` then modifies the order dict across multiple `await` points (fill processing, POSMANAGER send, GUIBROKER forward). Meanwhile, a cancel request from GUIBROKER could be processing the same order simultaneously. The `_positions` dict has a lock, but `self.orders` does not.
- **Recommendation**: Wrap order-book reads and writes in an `asyncio.Lock` (similar to `_positions_lock`), or use a single-writer pattern to ensure execution report updates and new order processing do not interleave on the same order.

### C-3: `_om_connected` flag read/written without synchronization -- messages silently queued forever (GUIBROKER)

- **File**: `guibroker/guibroker.py`, lines 78, 315, 321, 356, 363-364
- **Description**: `_om_connected` is set to `True` on line 356 during `_om_connect_and_listen`, and set to `False` on line 321 in `_send_to_om` (on error) and on line 363 in the `finally` block. The `_send_to_om` method (line 315) checks `self._om_connected and self._om_client._ws` as a guard. Since this is purely flag-based with no lock, there is a window where `_om_connected` is `True` but the websocket is stale (e.g. the connection dropped but `_om_connect_and_listen` hasn't reached the `finally` block yet). In this window, `send` will raise an exception, which sets `_om_connected = False` and queues the message. More critically, if the OM reconnects but `_flush_pending_queue` fails partway through, messages can be permanently lost or reordered. Also, `_send_to_om` accesses the internal `_om_client._ws` attribute directly, bypassing the `WSClient` abstraction.
- **Recommendation**: Use an `asyncio.Lock` around the connected state and queue flush operations. Replace `self._om_client._ws` with a proper `is_connected()` method on `WSClient`.

---

## WARNING Issues

### W-1: Dead code -- redundant `price = float(...)` assignment

- **File**: `om/order_manager.py`, line 100
- **Description**: `price` is already assigned on line 97. The `try/except` block on lines 99-102 re-assigns `price` to the exact same expression. The except clause would only be reached if the `float()` on line 100 fails, but by that point line 97 already failed (or succeeded). This means the `try/except ValueError` guard on line 100 is dead code -- it can never catch an error that wasn't already raised on line 97.
- **Recommendation**: Remove the redundant assignment on line 97 and keep only the guarded one on lines 99-102.

### W-2: Unsafe `float()` on price for market orders

- **File**: `om/order_manager.py`, line 196
- **Description**: `price = float(fix_msg.get(Tag.Price, "0"))` can raise `ValueError` if the tag contains a non-numeric string. For market orders, `Tag.Price` may not be set by the factory function `new_order_single` (it skips setting price for market orders), so the default "0" is fine. However, if a malformed FIX message arrives with a garbage price tag, this line will crash.
- **Recommendation**: Wrap in `try/except ValueError` or validate earlier.

### W-3: Unsafe `float()` conversions in GUIBROKER `_handle_new_order`

- **File**: `guibroker/guibroker.py`, lines 141, 143
- **Description**: `qty = float(msg.get("qty", 0))` and `price = float(msg.get("price", 0))` can raise `ValueError` if the GUI sends non-numeric strings. The outer `try/except Exception` on line 133 will catch this, but the error message sent back to GUI (`Processing error: ...`) is generic and unhelpful.
- **Recommendation**: Validate qty and price explicitly before conversion and return a clear error to the GUI.

### W-4: Unsafe `float()` conversions in GUIBROKER `_handle_amend_order`

- **File**: `guibroker/guibroker.py`, lines 213, 214
- **Description**: Same issue as W-3 for the amend order handler.
- **Recommendation**: Same as W-3.

### W-5: Accessing private attribute `_om_client._ws` in GUIBROKER

- **File**: `guibroker/guibroker.py`, line 315
- **Description**: `self._om_connected and self._om_client._ws` directly accesses the internal `_ws` attribute of `WSClient`. This is fragile -- if the `WSClient` implementation changes, this breaks silently.
- **Recommendation**: Add a public `is_connected` property to `WSClient` and use it instead.

### W-6: Accessing private attribute `_om_client._ws` in GUIBROKER cleanup

- **File**: `guibroker/guibroker.py`, line 364
- **Description**: `self._om_client._ws = None` directly manipulates `WSClient` internals in the reconnect loop's `finally` block. This bypasses any cleanup logic inside `WSClient`.
- **Recommendation**: Use `await self._om_client.close()` or add a `reset()` method.

### W-7: Accessing private attribute `_client._connected` in CoinbaseFIXAdapter

- **File**: `exchconn/coinbase_fix_adapter.py`, lines 128, 185, 230
- **Description**: `self._client._connected` directly accesses the internal state of `FIXClient`. If `FIXClient` renames this attribute, these checks break silently.
- **Recommendation**: Add a public `is_connected` property to `FIXClient`.

### W-8: Dynamic attribute assignment on `_TrackedOrder` without `__slots__` or class-level declaration

- **File**: `exchconn/coinbase_fix_adapter.py`, lines 266-267
- **Description**: `self._orders[cl_ord_id]._amend_fallback_orig = orig_cl_ord_id` and `_amend_fallback_msg = fix_msg` dynamically add attributes to `_TrackedOrder` instances that are not declared in `__init__`. The `hasattr(tracked, "_amend_fallback_orig")` check on line 358 relies on this. This is fragile and confusing.
- **Recommendation**: Add `_amend_fallback_orig` and `_amend_fallback_msg` as `Optional` attributes in `_TrackedOrder.__init__`.

### W-9: `FIXSession.next_send_seq()` is not thread-safe / async-safe

- **File**: `shared/fix_engine.py`, lines 136-138
- **Description**: `_send_seq += 1` is a non-atomic read-modify-write. If multiple async tasks call `send()` concurrently on the same `FIXClient`, they could get duplicate sequence numbers. In practice, the `FIXClient.send()` goes through `_send_raw()` which uses `self._writer.write()` + `drain()`, but there is no lock ensuring atomicity of the stamp+write sequence.
- **Recommendation**: Add an `asyncio.Lock` around `stamp_message` + `_send_raw` in `FIXClient.send()`.

### W-10: `FIXSession.advance_recv_seq()` ignores the `expected` parameter

- **File**: `shared/fix_engine.py`, line 140-141
- **Description**: The method signature accepts an `expected` parameter but never uses it. The sequence number simply increments by 1. This means gap detection is not implemented -- if messages arrive out of order or with gaps, the session will not detect it.
- **Recommendation**: Either implement gap detection using `expected`, or remove the parameter to avoid confusion.

### W-11: `FIXClient._close_socket` does not await `_writer.wait_closed()`

- **File**: `shared/fix_engine.py`, lines 518-528
- **Description**: `_close_socket` calls `self._writer.close()` but never calls `await self._writer.wait_closed()`. Per Python docs, `close()` is not guaranteed to complete immediately -- `wait_closed()` should be awaited to ensure the transport is fully closed. However, since `_close_socket` is a sync method, it cannot `await`. This means the socket may not be fully cleaned up.
- **Recommendation**: Convert `_close_socket` to an async method and await `wait_closed()`, or use a synchronous-safe pattern.

### W-12: `_write_status` has a TOCTOU race on the status JSON file

- **File**: `shared/fix_engine.py`, lines 531-556
- **Description**: The method reads the existing status file, modifies it, and writes it back. If two `FIXClient` instances (e.g., FIX-ORD and FIX-MD) call `_write_status` simultaneously from different processes or async tasks, one write can overwrite the other's data. This is a classic Time-of-Check-to-Time-of-Use (TOCTOU) race.
- **Recommendation**: Use file locking (`fcntl.flock` on Unix) or write to separate per-client status files.

### W-13: `load_limits()` returns a shallow copy of `DEFAULT_RISK_LIMITS`

- **File**: `shared/risk_limits.py`, line 25
- **Description**: `dict(DEFAULT_RISK_LIMITS)` creates a shallow copy. The nested `max_order_qty` and `max_position_qty` dicts are shared references. If a caller modifies the returned dict's nested values, it mutates the module-level `DEFAULT_RISK_LIMITS` for all future calls.
- **Recommendation**: Use `copy.deepcopy(DEFAULT_RISK_LIMITS)` or `json.loads(json.dumps(DEFAULT_RISK_LIMITS))`.

### W-14: `save_limits()` does not validate input

- **File**: `shared/risk_limits.py`, lines 28-31
- **Description**: `save_limits(limits)` writes arbitrary dict content to `risk_limits.json` without validating the schema. A malformed dict from the GUI POST handler could corrupt the limits file, causing `check_order()` to fail on subsequent calls.
- **Recommendation**: Validate that the dict has the expected keys and value types before writing.

### W-15: `WSServer.broadcast` sends to a snapshot of clients but doesn't handle clients leaving mid-broadcast

- **File**: `shared/ws_transport.py`, lines 64-71
- **Description**: `self.clients - {exclude}` takes a snapshot, but `asyncio.gather` with `return_exceptions=True` means failed sends are silently swallowed. While `return_exceptions=True` prevents crashes, the exceptions are never logged, making it impossible to debug connection issues.
- **Recommendation**: Log or inspect the results of `asyncio.gather` for exceptions.

### W-16: `WSClient.listen()` re-enters `connect()` without clearing stale `_ws`

- **File**: `shared/ws_transport.py`, lines 132-148
- **Description**: In the `listen()` method, when a `ConnectionClosed` exception occurs, `self._ws` is set to `None` on line 143. On the next loop iteration, `connect()` is called. But if `connect()` was already called before `listen()` (which is normal), the existing `_ws` connection object from the initial `connect()` call is not explicitly closed -- it's just overwritten in `connect()`.
- **Recommendation**: Ensure the old `_ws` is explicitly closed before reconnecting.

### W-17: `PubSub.unsubscribe` raises `ValueError` if handler not found

- **File**: `shared/ws_transport.py`, line 172
- **Description**: `self._subscribers[topic].remove(handler)` raises `ValueError` if the handler is not in the list. This would propagate as an unhandled exception.
- **Recommendation**: Use a try/except or check `if handler in list` before removing.

### W-18: `message_store.store_message` opens a new SQLite connection for every message

- **File**: `shared/message_store.py`, lines 67-102
- **Description**: Every call to `store_message` creates a new `sqlite3.connect()`, writes one row, commits, and closes. This is extremely inefficient for high-throughput message logging (market data ticks happen multiple times per second per symbol). SQLite connection creation is expensive.
- **Recommendation**: Use a connection pool, or keep a persistent connection per thread/process with WAL mode.

### W-19: `message_store.cleanup()` called outside the lock on line 100

- **File**: `shared/message_store.py`, lines 95-102
- **Description**: `_insert_count` is incremented and checked inside `_insert_lock`, but `cleanup()` is called *inside* the lock. This means the lock is held during the potentially slow `cleanup()` DELETE operation, blocking all other `store_message` calls. However, the lock is a `threading.Lock` (not asyncio), and the code runs in async context, which could cause issues if called from an event loop thread.
- **Recommendation**: Call `cleanup()` outside the lock, or use an `asyncio.Lock` since this runs in async context.

### W-20: `gui/server.py` do_POST for `/api/troubleshoot` -- SSE stream error handling is fragile

- **File**: `gui/server.py`, lines 283-341
- **Description**: The troubleshoot endpoint sends `send_response(200)` and starts streaming SSE *before* the Anthropic API call completes. If the API call fails after headers are sent, the error handling on lines 333-341 tries to write an error message to the stream, but the HTTP response code is already 200. The client sees a 200 response with an error payload, which is confusing.
- **Recommendation**: Buffer the response or use a proper SSE error event format.

### W-21: `gui/server.py` -- `do_POST` for `/api/risk-limits` does not handle `Content-Length` of 0

- **File**: `gui/server.py`, line 265
- **Description**: `int(self.headers.get("Content-Length", 0))` returns 0 if the header is missing. `self.rfile.read(0)` returns `b""`, and `json.loads(b"")` raises `json.JSONDecodeError`. This is caught by the except on line 271, but the error message is unhelpful ("Expecting value: line 1 column 1 (char 0)").
- **Recommendation**: Check for zero-length body explicitly and return a clear error.

### W-22: `posmanager/posmanager.py` -- `_schedule_broadcast` uses `asyncio.ensure_future` inside `call_later`

- **File**: `posmanager/posmanager.py`, lines 304-305
- **Description**: `asyncio.get_running_loop().call_later(delay, lambda: asyncio.ensure_future(self._delayed_broadcast()))` creates a future inside a `call_later` callback. If the `_delayed_broadcast` coroutine raises an exception, the future is fire-and-forget -- the exception is silently lost. Also, `call_later` callbacks run outside the context of any await, so errors are harder to trace.
- **Recommendation**: Store the returned `Future` object and add a done callback to log exceptions, or use `asyncio.create_task` instead and store the task reference.

### W-23: `posmanager/posmanager.py` -- `_do_broadcast` re-parses JSON it just created

- **File**: `posmanager/posmanager.py`, lines 316-318
- **Description**: `_do_broadcast` calls `_build_position_update()` which `json.dumps()` the positions, then immediately calls `json.loads(msg)` to check if the positions list is empty. This is an unnecessary serialize-deserialize round-trip.
- **Recommendation**: Refactor `_build_position_update` to return both the raw list and the JSON string, or check the list before serializing.

### W-24: `om/order_manager.py` -- signal handler uses `asyncio.ensure_future` which is fire-and-forget

- **File**: `om/order_manager.py`, line 654
- **Description**: `asyncio.ensure_future(om.shutdown())` in the signal handler creates a coroutine that is not awaited. If `shutdown()` raises an exception, it will be silently lost.
- **Recommendation**: Store the returned future/task and add a done callback for error logging.

### W-25: `guibroker/guibroker.py` -- `_shutdown` function cancels all tasks including itself

- **File**: `guibroker/guibroker.py`, lines 402-408
- **Description**: `_shutdown` filters out `asyncio.current_task()` but then calls `loop.stop()`. Since `_shutdown` itself is a task created via `asyncio.ensure_future`, calling `loop.stop()` from inside a running task is fragile -- it may prevent the gather from completing cleanly.
- **Recommendation**: Use `loop.call_soon(loop.stop)` to schedule the stop after the current iteration.

---

## INFO Issues

### I-1: `FIXMessage.decode` does not validate checksum

- **File**: `shared/fix_protocol.py`, line 140-148
- **Description**: The `decode` class method parses the checksum tag but does not verify it against the computed checksum. This means corrupted messages are accepted silently.
- **Recommendation**: Add checksum validation for defense in depth.

### I-2: `FIXMessage.encode` computes checksum over joined body but the checksum field's separator differs

- **File**: `shared/fix_protocol.py`, lines 128-137
- **Description**: The `encode` method uses `"|"` as the separator. The checksum is computed over the body *without* the final `|` before the checksum field. When the full message is reconstructed with `"|".join(parts)`, the checksum is appended with a `|` separator. This is consistent internally but differs from real FIX protocol which uses SOH. Since this is an internal transport format (real FIX uses `fix_engine.py`), this is only informational.
- **Recommendation**: No action needed for internal use, but document the custom format.

### I-3: `coinbase_adapter.py` -- duplicate code for market buy and sell

- **File**: `exchconn/coinbase_adapter.py`, lines 174-190
- **Description**: The `if cb_side == "BUY"` and `else` branches on lines 174-190 contain identical code. The comment on line 175 says "Market buy requires quote_size (USD amount)" but then uses `base_size` anyway, identical to the sell branch.
- **Recommendation**: Remove the duplicate branch and handle both buy and sell with the same code, or implement the quote_size logic for market buys.

### I-4: `restart.py` uses Windows-specific commands

- **File**: `restart.py`, lines 33-36, 67-69
- **Description**: `restart.py` uses `netstat -ano` and `taskkill /F /PID` which are Windows-specific. This script will fail on Linux/macOS (the environment is WSL Linux).
- **Recommendation**: Add platform detection and use `lsof` / `kill` on Unix systems.

---

## Pass/Fail Counts

| Severity | Count |
|----------|-------|
| CRITICAL | 3 |
| WARNING  | 25 |
| INFO     | 4 |
| **Total**| **32** |

### Verdict

**3 CRITICAL issues require attention before production use.**

The most impactful issues are:
1. **C-1**: Unhandled `ValueError` crash on malformed price input in OM's order validation -- a single malformed FIX message from GUIBROKER can crash the entire order handler.
2. **C-2**: Order book race condition in OM -- concurrent execution report processing and new order/cancel handling on the same order can produce inconsistent state.
3. **C-3**: GUIBROKER's OM connection state management is fragile -- messages can be silently lost or permanently queued during reconnection windows.
