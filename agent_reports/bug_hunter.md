# Bug Hunter Agent Report

## Date: 2026-03-01

## Summary

Static analysis of 26 Python source files. Many issues from the prior round have been fixed: the OM orders lock is now used consistently, terminal order cleanup exists, the Binance/Coinbase simulator amend bugs are resolved, and fire-and-forget patterns now store tasks and log exceptions. This analysis found 10 remaining findings -- none critical, but several high-severity issues around concurrency, coupling, and error handling.

## Findings

### [HIGH] OM execution report handler mutates order dict outside the lock

- **Category**: Concurrency (Learned Theme: shared mutable state across await boundaries)
- **Location**: `om/order_manager.py`:566-677
- **Issue**: `_handle_execution_report` acquires `self._orders_lock` to look up the order (lines 566-572) but then releases it before mutating the order dict's fields (lines 583-654) and before the `await` calls to send to GUIBROKER (line 665) and schedule cleanup (line 674). The order object is a plain dict stored in `self.orders` -- another concurrent handler (e.g., a new GUIBROKER cancel request or a second fill from EXCHCONN for the same order) can interleave between the dict reads and writes.
  - Specifically, `order["status"]` is set on line 583, then execution-type-specific mutations happen (lines 585-658), and only then is the exec report forwarded. A second fill arriving between these steps could corrupt `cum_qty`, `avg_px`, or `leaves_qty`.
- **Impact**: In the common case (single fill per order), this is safe. With partial fills from the simulator (1-3 chunks), two fill reports arriving in rapid succession could interleave and produce inconsistent order state, especially `avg_px` calculations.
- **Suggested Fix**: Hold `self._orders_lock` for the entire duration of `_handle_execution_report`, including the mutations. The `await` calls for forwarding to GUIBROKER/POSMANAGER should be done after releasing the lock, using a local copy of the data needed for the messages.

### [HIGH] OM `_cleanup_terminal_order` uses fire-and-forget `asyncio.ensure_future` from `call_later` callback

- **Category**: Async Pitfalls (Learned Theme: fire-and-forget from call_later callbacks)
- **Location**: `om/order_manager.py`:674-677
- **Issue**: The code schedules terminal order cleanup via:
  ```python
  asyncio.get_running_loop().call_later(
      60, lambda cid=cl_ord_id, oid=order["order_id"]: asyncio.ensure_future(
          self._cleanup_terminal_order(cid, oid)
      )
  )
  ```
  `asyncio.ensure_future()` from a `call_later` callback returns a future that is not stored. If `_cleanup_terminal_order` raises an exception, it will be silently lost. This matches the Learned Theme about fire-and-forget coroutines from `call_later` callbacks.
- **Impact**: If the lock acquisition in `_cleanup_terminal_order` fails or the dict operations error, the exception is silently discarded. The terminal order remains in memory permanently.
- **Suggested Fix**: Store the task and add a done_callback for exception logging:
  ```python
  def _schedule_cleanup():
      task = asyncio.ensure_future(self._cleanup_terminal_order(cid, oid))
      task.add_done_callback(
          lambda t: logger.error(f"Cleanup error: {t.exception()}")
          if not t.cancelled() and t.exception() else None
      )
  asyncio.get_running_loop().call_later(60, _schedule_cleanup)
  ```

### [HIGH] Private attribute access `self._client._connected` in CoinbaseFIXAdapter

- **Category**: Coupling (Learned Theme: private attribute access across module boundaries)
- **Location**: `exchconn/coinbase_fix_adapter.py`:130, 187, 229
- **Issue**: `CoinbaseFIXAdapter` directly accesses `self._client._connected` (a private attribute of `FIXClient`) in `submit_order`, `cancel_order`, and `amend_order`. `FIXClient` already provides a public `is_connected` property (line 242 of `fix_engine.py`) that returns the same value.
- **Impact**: If `FIXClient` renames or restructures `_connected`, these accesses silently break. Changes to the public API would be caught; changes to private internals would not.
- **Suggested Fix**: Replace all three occurrences of `self._client._connected` with `self._client.is_connected`.

### [MEDIUM] Private attribute access `wire._fields` in CoinbaseFIXFeed

- **Category**: Coupling (Learned Theme: private attribute access across module boundaries)
- **Location**: `mktdata/coinbase_fix_feed.py`:130-136, 253
- **Issue**: `CoinbaseFIXFeed._on_connected` directly appends to `wire._fields` (a private list of `FIXWireMessage`) using `wire._fields.append((269, "0"))` to add repeating group entries. `_parse_md_entries` also iterates `wire._fields`. The `FIXWireMessage.set()` method replaces the first occurrence of a tag, which is correct for non-repeating fields but cannot add multiple entries with the same tag. The direct `_fields` access is a workaround for a missing public API.
- **Impact**: If `FIXWireMessage` changes its internal storage, this code silently breaks.
- **Suggested Fix**: Add a public `add(tag, value)` method to `FIXWireMessage` for appending repeating group fields, and a public `fields` property or iterator for reading all fields.

### [MEDIUM] `coinbase_adapter.py` -- duplicate BUY/SELL branches for market orders

- **Category**: Logic Errors / Dead Code
- **Location**: `exchconn/coinbase_adapter.py`:174-190
- **Issue**: The `if cb_side == "BUY"` and `else` branches contain identical code. The comment says "Market buy requires quote_size (USD amount) -- use qty as a base-size workaround" but then uses `base_size` for both sides:
  ```python
  if cb_side == "BUY":
      # Market buy requires quote_size (USD amount) — use qty as a base-size workaround
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
  Either the BUY branch should use `quote_size` as the comment implies, or the branch is dead code.
- **Impact**: If Coinbase requires `quote_size` for market buys (which it does for certain order configurations), market buy orders may be rejected at runtime. If `base_size` is acceptable for both, the dead branch makes the code harder to maintain.
- **Suggested Fix**: Either implement `quote_size` for market buys (research Coinbase API docs), or collapse into a single code path and update the comment.

### [MEDIUM] `message_store` persistent SQLite connection not safe for multi-threaded use

- **Category**: Concurrency
- **Location**: `shared/message_store.py`:68-76, 79-113
- **Issue**: `_get_persistent_conn()` creates a single `sqlite3.Connection` stored in a module-level global without `check_same_thread=False`. The `_insert_lock` is a `threading.Lock` but only protects the insert counter (lines 103-107), not the `conn.execute()` and `conn.commit()` calls (lines 95-100). `gui/server.py` uses `socketserver.TCPServer` which is single-threaded by default, but if it is ever switched to `ThreadingTCPServer`, or if `store_message` is called concurrently from different threads, the unprotected shared connection will raise `sqlite3.ProgrammingError`.
- **Impact**: Currently low risk (single-threaded HTTP server), but a latent issue that would surface if threading is introduced.
- **Suggested Fix**: Either wrap the entire `store_message` database operations inside `_insert_lock`, OR use per-thread connections via `threading.local()`, OR pass `check_same_thread=False` to the connection and add locking.

### [MEDIUM] GUIBROKER `_om_connect_and_listen` reconnection silently resets `_om_connected` without draining queue

- **Category**: Error Handling / Logic Errors
- **Location**: `guibroker/guibroker.py`:378-395
- **Issue**: When the OM connection drops, the `finally` block sets `_om_connected = False` and calls `await self._om_client.close()`. However, `_om_client.close()` sets `self._running = False` inside WSClient, which prevents subsequent reconnection. The outer `while True` loop calls `self._om_client.connect()` again, but since `_running` was set to `False`, the `connect()` method (which checks `while self._running`) will exit immediately without actually connecting.

  Actually, looking more carefully: `WSClient.close()` sets `self._running = False` (line 170) and `WSClient.connect()` loops `while self._running` (line 121). After `close()`, the next `connect()` call starts with `self._running = True` (line 120), so this is correct.

  However, there is still a sequencing issue: `_flush_pending_queue` is called after setting `_om_connected = True` (line 384, 386). If `_flush_pending_queue` fails and throws, `_om_connected` remains `True` but the connection state is broken, and the `listen()` call on line 387 may also fail.
- **Impact**: If flush fails, the connection state flag and actual connection state diverge temporarily. Messages queued during this window may be lost.
- **Suggested Fix**: Wrap the flush+listen in a try block that resets `_om_connected` on failure before the `finally` clause runs.

### [LOW] `FIXSession._recv_seq` directly mutated from `FIXClient._dispatch`

- **Category**: Coupling
- **Location**: `shared/fix_engine.py`:393
- **Issue**: `FIXClient._dispatch` handles SequenceReset (MsgType=4) by directly setting `self.session._recv_seq = new_seq - 1`, bypassing the `advance_recv_seq()` public method. This couples the dispatch logic to the internal counter implementation of `FIXSession`.
- **Impact**: If `FIXSession` changes how sequence numbers are tracked (e.g., adding validation or logging), this direct mutation will bypass it.
- **Suggested Fix**: Add a `reset_recv_seq(new_seq)` method to `FIXSession` and use it from `_dispatch`.

### [LOW] OM `_next_order_id` uses non-atomic read-modify-write counter

- **Category**: Concurrency
- **Location**: `om/order_manager.py`:75-78
- **Issue**: `_next_order_id` does `self._order_id_counter += 1` without any lock. Currently safe because it is a synchronous method called from async contexts without an `await` between the call and use of the result. However, GUIBROKER uses `itertools.count(1)` (line 71 of guibroker.py) for the same purpose, which is inherently atomic.
- **Impact**: Currently safe; maintenance hazard if an `await` is introduced.
- **Suggested Fix**: Use `itertools.count(1)` for consistency with GUIBROKER.

### [LOW] `gui/server.py` POST handler for `/api/risk-limits` may send `resp` undefined if `json.loads` fails mid-path

- **Category**: Variable Safety
- **Location**: `gui/server.py`:262-288
- **Issue**: In the `do_POST` handler for `/api/risk-limits`, the `try` block on line 264 creates `resp` only inside the `try`/`except` blocks. However, the `self.send_header()` and `self.end_headers()` calls after the try/except (lines 285-288) are outside any error handling. If an exception type not caught by the except clauses (e.g., `UnicodeDecodeError`) is raised after `self.send_response()` but before `resp` is assigned, the code falls through to line 288 where `resp.encode("utf-8")` would raise `NameError` because `resp` is undefined.

  However, looking more carefully, the current code structure has `self.send_response()` inside each branch of the try/except, and the subsequent `send_header`/`end_headers`/`wfile.write` calls are always reached with `resp` defined in all three branches (try success, ValueError/JSONDecodeError, OSError). The only gap is if an unexpected exception type (not caught) is raised -- but in that case, Python's default error handler would catch it. Still, the pattern is fragile.
- **Impact**: Very low risk -- would require an uncaught exception type.
- **Suggested Fix**: Move the common `send_header`/`end_headers`/`wfile.write` into each branch, or use a `finally` block with a default response.

## Previously Fixed Issues (Verified)

The following issues from the prior report have been verified as resolved:

1. **[CRITICAL] BinanceSimulator.amend_order wrong argument type** -- Fixed. Lines 440-445 now correctly pass `order` (SimulatedOrder) and `fill_price` to `_execute_fill`.
2. **[CRITICAL] CoinbaseSimulator.amend_order wrong argument type** -- Fixed. Lines 429-434 now correctly pass `order` and `fill_price`.
3. **[HIGH] OM orders dict accessed without lock** -- Fixed. `self._orders_lock` is now acquired in all code paths: `_handle_guibroker_connect` (line 148), `_validate_order` via `_handle_new_order` (line 188), `_handle_new_order` insert (line 238), cancel/amend handlers (lines 280, 303, 336, 475), `_handle_order_status_request` (line 506), `_handle_execution_report` (line 566), and `_cleanup_terminal_order` (line 682).
4. **[HIGH] OM order book grows without bound** -- Fixed. `_cleanup_terminal_order` method (lines 680-687) removes orders 60 seconds after reaching terminal state, scheduled via `call_later` (line 674).
5. **[HIGH] GUIBROKER fire-and-forget shutdown task** -- Improved. Signal handler now stores task and adds done_callback with `not t.cancelled()` guard (lines 426-429).
6. **[MEDIUM] `_flush_pending_queue` not holding lock** -- Fixed. Now acquires `self._om_lock` (line 355).
7. **[MEDIUM] POSMANAGER delayed broadcast CancelledError** -- Fixed. Done callback now guards with `not t.cancelled()` (line 311).
8. **Unbound `price` variable in OM `_validate_order`** -- Fixed. Price initialized to `0.0` on line 98 before conditionals.

## No Issues Found

The following categories were checked and found clean:

- **Missing await**: All coroutine calls are properly awaited.
- **Fire-and-forget tasks (general)**: Most `asyncio.create_task()` calls store the returned task with cleanup in `stop()` methods.
- **Off-by-one errors**: Loop bounds and slice indices correct throughout.
- **Incorrect operator usage**: No `=` vs `==`, `and` vs `or`, or `is` vs `==` misuse found.
- **FIX engine send atomicity**: `FIXClient.send()` correctly uses `self._send_lock` for atomic stamp+write.
- **FIX engine status file locking**: `_write_status` uses `fcntl.flock(LOCK_EX)` correctly.
- **`risk_limits.load_limits()` deep copy**: Correctly uses `copy.deepcopy()`.
- **`risk_limits.save_limits()` validation**: Validates keys and types before writing.
- **Resource leaks in WSClient**: `listen()` method properly closes WebSocket in error paths.
- **FIXClient `_close_socket`**: Correctly async with `await self._writer.wait_closed()`.
