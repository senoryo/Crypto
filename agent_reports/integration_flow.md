# Integration Flow Agent Report

**Date**: 2026-03-01 (second pass)
**Agent**: Integration Flow Validator
**Scope**: guibroker/guibroker.py, om/order_manager.py, exchconn/exchconn.py, exchconn/binance_sim.py, exchconn/coinbase_sim.py, posmanager/posmanager.py, mktdata/mktdata.py, proxy.py, gui/app.js, shared/fix_protocol.py, shared/ws_transport.py

---

## Flow 1: New Order (GUI -> GUIBROKER -> OM -> EXCHCONN -> Exchange)

### Trace

1. **GUI** (`app.js:329-370`): User submits order form. Builds JSON `{type: "new_order", symbol, side, qty, ord_type, price, exchange}` and sends to GUIBROKER WebSocket.
2. **GUIBROKER** (`guibroker.py:138-191`): Parses JSON, assigns `ClOrdID` (GUI-1, GUI-2, ...), maps `side`/`ord_type` strings to FIX enums, builds `NewOrderSingle` FIX message via `new_order_single()` factory. Stores `cl_ord_id -> websocket` mapping. Sends `order_ack` JSON back to GUI, then forwards FIX to OM.
3. **OM** (`order_manager.py:183-273`): Parses FIX, validates order (symbol, qty, price, exchange, risk limits). If rejected, sends `ExecutionReport(Rejected)` back to GUIBROKER. If accepted, assigns OM order ID (`OM-000001`), creates order book entry with `source_ws`, resolves exchange, sets `ExDestination`, forwards to EXCHCONN.
4. **EXCHCONN** (`exchconn.py:88-150`): Parses FIX, determines exchange from `ExDestination` tag or default routing. Performs defense-in-depth qty sanity check. Routes to simulator's `submit_order()`.
5. **Exchange Simulator** (`binance_sim.py:160-198` / `coinbase_sim.py:158-196`): Assigns exchange order ID (BIN-/CB- prefix), creates `SimulatedOrder`, sends immediate `ExecutionReport(New)` ack, schedules fill task for market orders.

### Issues Found

**ISSUE 1 (Low): `order_ack` sent from GUIBROKER lacks `ord_type` and `exchange` fields**

At `guibroker.py:178-186`, the `order_ack` JSON includes `cl_ord_id, symbol, side, qty, price` but omits `ord_type` and `exchange`. The GUI at `app.js:851-868` uses `data.ord_type` and `data.exchange` from the ack to pre-populate the blotter row. Both default to `""`, leaving ord_type and exchange columns blank until the first ExecutionReport arrives.

**ISSUE 2 (Low): Exchange simulators do not include `OrderQty` in New ack execution reports**

The `execution_report()` factory at `fix_protocol.py:192-229` only includes `OrderQty` if the `order_qty` parameter is non-zero (line 225: `if order_qty:`). In the New order ack from exchange simulators (`binance_sim.py:181-192`), `order_qty` is not passed. GUIBROKER reads `Tag.OrderQty` with default `"0"` (line 301), so the GUI sees qty=0 on the initial New ack. The correct qty only appears when the GUI merges with the `order_ack` data or when fill reports arrive. This was identified in prior reports and partially addressed -- the GUIBROKER now reads with fallback, and the GUI merges exec reports with existing order state from the `order_ack`.

---

## Flow 2: Execution Report (Exchange -> EXCHCONN -> OM -> GUIBROKER -> GUI)

### Trace

1. **Exchange Simulator** (`binance_sim.py:200-283`): Generates fills (possibly partial, in chunks with delays). Calls `_send_report()` which sets `ExDestination` tag and invokes the EXCHCONN callback.
2. **EXCHCONN** (`exchconn.py:154-168`): `_on_execution_report()` receives FIX message from simulator. Sends to all connected OM clients. If no OM clients connected, queues in `_pending_reports` (maxlen=1000).
3. **OM** (`order_manager.py:560-678`): `_handle_execution_report()` looks up order by `cl_ord_id` or reverse-lookup by `order_id`. Updates order book (status, cum_qty, leaves_qty, avg_px). For fills, sends fill notification to POSMANAGER. Forwards the FIX exec report to GUIBROKER via `source_ws`.
4. **GUIBROKER** (`guibroker.py:274-333`): `_handle_execution_report()` maps `cl_ord_id` back to original for cancel/amend cases. Converts FIX fields to JSON. Routes to the specific GUI client by looking up `_client_orders[cl_ord_id]`.
5. **GUI** (`app.js:378-423`): `onExecutionReport()` merges fields into `state.orders` Map, records fill in trade history, re-renders blotter.

### Issues Found

**ISSUE 3 (Medium): Execution report for unknown order in OM is silently swallowed**

At `order_manager.py:574-580`, if an execution report arrives from EXCHCONN for an order not in the OM's book (either never existed or was cleaned up by `_cleanup_terminal_order` after 60 seconds), the OM logs a warning but does NOT forward anything to GUIBROKER. The message is completely lost. This can happen if EXCHCONN replays queued reports after a reconnection for orders that the OM has already cleaned up.

**ISSUE 4 (Medium): OM terminal order cleanup removes entries but leaves alias ClOrdIDs leaking**

At `order_manager.py:672-687`, terminal orders are cleaned up after 60 seconds. However, `_cleanup_terminal_order` only removes `self.orders[cl_ord_id]` (the original ClOrdID) and `_om_id_to_cl_ord_id[order_id]`. Any alias entries created during cancel/amend flows (lines 304-305, 476-477: `self.orders[new_cl_ord_id] = order`) are never removed. These alias keys reference an order dict that may still exist (if the alias points to the same object) or may be stale. Over time with many cancel/amend operations, `self.orders` accumulates entries that are never cleaned up.

---

## Flow 3: Cancel Order (GUI -> GUIBROKER -> OM -> EXCHCONN -> Exchange -> Response back)

### Trace

1. **GUI** (`app.js:571-590`): Builds `{type: "cancel_order", cl_ord_id, symbol, side}`, sends to GUIBROKER.
2. **GUIBROKER** (`guibroker.py:193-214`): Assigns new `ClOrdID`, stores `_cancel_to_orig` and `_client_orders` mappings. Builds FIX `OrderCancelRequest`. Sends to OM.
3. **OM** (`order_manager.py:275-329`): Looks up original order by `OrigClOrdID`. If not found, sends reject. Maps new ClOrdID to same order dict. Forwards to EXCHCONN. On forwarding failure, sends reject back.
4. **EXCHCONN** routes to exchange simulator's `cancel_order()`.
5. **Exchange Simulator** (`binance_sim.py:285-354`): Looks up order by `OrigClOrdID`. Cancels fill tasks. Sends `ExecutionReport(Canceled)`.
6. **Response back**: Flows through EXCHCONN -> OM -> GUIBROKER. GUIBROKER maps cancel ClOrdID back to original via `_cancel_to_orig`.

### Issues Found

Cancel flow traces clean. Previously reported issues (cancel forwarding failure not producing reject) have been fixed.

---

## Flow 4: Amend Order (GUI -> GUIBROKER -> OM -> EXCHCONN -> Exchange -> Response back)

### Trace

1. **GUI** (`app.js:615-646`): Builds `{type: "amend_order", cl_ord_id, symbol, side, qty, price}`.
2. **GUIBROKER** (`guibroker.py:216-251`): Assigns new ClOrdID, builds FIX `OrderCancelReplaceRequest`. Forwards to OM.
3. **OM** (`order_manager.py:331-501`): Validates, risk-checks amended values. Forwards to EXCHCONN. On failure, sends reject.
4. **EXCHCONN** (`exchconn.py:133-147`): Defense-in-depth qty check. Routes to `amend_order()`.
5. **Exchange Simulator** (`binance_sim.py:356-446`): Updates qty/price, updates cl_ord_id mapping. Sends `ExecutionReport(Replaced)` with `OrderQty` and `Price` tags. Restarts fill task for market orders.
6. **Response back**: OM updates order book with new qty/price from Replaced report (lines 633-646). GUIBROKER maps back to original ClOrdID.

### Issues Found

**ISSUE 5 (HIGH): Amended price is NOT forwarded to the GUI in the execution report JSON**

At `guibroker.py:293-309`, the `json_report` dict built from the FIX execution report includes many fields but does **not** include a `"price"` key. While the exchange simulator sets `Tag.Price` on the Replaced exec report (`binance_sim.py:436`), and the OM forwards the FIX message as-is, the GUIBROKER translation to JSON at lines 293-309 has no line that reads `Tag.Price` and puts it into the JSON. The GUI at `app.js:396` reads `data.price` which will be `undefined`, and the ternary `data.price != null ? data.price : (existing.price || 0)` evaluates to `undefined != null` which is `false`, so it falls back to `existing.price` (the old price). **However**, this means the old price is preserved, not overwritten to 0. So the display shows the **original** price, not the **amended** price. The user cannot see that their amend changed the price.

Note: `qty` IS forwarded correctly because GUIBROKER includes `"qty": _safe_float(fix_msg.get(Tag.OrderQty, "0"))` at line 301, and the Replaced report includes `Tag.OrderQty`.

**ISSUE 6 (Low): Amend on limit order does not trigger immediate fill re-evaluation in exchange simulators**

At `binance_sim.py:440-446` and `coinbase_sim.py:429-434`, after an amend, fill tasks are only restarted for market orders. For limit orders with an amended price, the order must wait for the next `_check_limit_fills()` cycle (every 0.5s). This is a design choice, not a bug.

---

## Flow 5: Fill Notification (OM -> POSMANAGER)

### Trace

1. **OM** (`order_manager.py:612-627`): After processing a Trade exec report, builds JSON: `{type: "fill", symbol, side, qty, price, cl_ord_id, order_id}`. Side is converted from FIX ("1"/"2") to human-readable ("BUY"/"SELL"). Sends to POSMANAGER via WebSocket client.
2. **POSMANAGER** (`posmanager.py:177-238`): Parses fill, calls `Position.apply_fill()`, updates qty/avg_cost/realized_pnl. Schedules throttled broadcast.

### Issues Found

**ISSUE 7 (HIGH): Failed fill sends to POSMANAGER are silently lost, causing position divergence**

At `order_manager.py:623-627`, if `self.pos_client.send()` raises an exception, the error is logged but the fill is never retried or queued. At `ws_transport.py:136-138`, `WSClient.send()` silently does nothing if `self._ws is None` (no exception raised). So if POSMANAGER is disconnected, fill notifications are simply discarded.

The fill has already been applied to the OM's internal position tracking (lines 605-610). So the OM's position state reflects the fill, but POSMANAGER's state does not. This divergence is permanent -- there is no reconciliation mechanism.

Compare: GUIBROKER has `_pending_queue` for OM. EXCHCONN has `_pending_reports` for OM. But OM has **no** queuing for POSMANAGER.

**ISSUE 8 (Low): No fill deduplication in POSMANAGER**

Fill messages have no idempotency key or sequence number. If retry logic were added to fix Issue 7, POSMANAGER could double-count fills without deduplication. The `_fill_sequence` counter at `posmanager.py:226` is informational only.

---

## Flow 6: Market Data (MKTDATA -> POSMANAGER, MKTDATA -> GUI)

### Trace

1. **MKTDATA** (`mktdata.py:131-158`): Receives ticks from feed simulators via callback. Caches latest per (symbol, exchange). Broadcasts to subscribed clients.
2. **POSMANAGER** (`posmanager.py:240-278`): Connected as WS client. Updates `Position.market_price`. Schedules throttled broadcast of position updates.
3. **GUI** (`app.js:144-165`): Connected directly to MKTDATA. Updates market data grid.

### Issues Found

**ISSUE 9 (Medium): MKTDATA sends market data to clients sequentially, not using WSServer.broadcast()**

At `mktdata.py:151-158`, `_on_market_data()` iterates over `self._subscriptions` and calls `await ws.send(message)` for each client individually in a sequential loop. The `WSServer.broadcast()` method at `ws_transport.py:64-74` uses `asyncio.gather()` for parallel sends, but MKTDATA does not use it because it needs to check per-client subscriptions. A slow client blocks all subsequent sends in the loop. For high-frequency market data, this could cause latency spikes.

---

## Flow 7: Disconnect/Reconnect and Message Queuing

### Trace

| Path | Queuing | Replay on Reconnect |
|------|---------|---------------------|
| GUIBROKER -> OM | `_pending_queue` (unbounded deque) | Yes, `_flush_pending_queue()` |
| EXCHCONN -> OM | `_pending_reports` (deque, maxlen=1000) | Yes, replay on connect |
| OM -> EXCHCONN | WSClient auto-reconnect; on send failure, reject back to GUIBROKER | N/A |
| OM -> POSMANAGER | **None** | **No** -- fills silently lost |
| POSMANAGER -> MKTDATA | WSClient auto-reconnect; snapshots sent on connect | Snapshots only |
| GUI -> GUIBROKER | Browser auto-reconnect (3s delay) | No order state snapshot |
| GUI -> MKTDATA | Browser auto-reconnect (3s delay) | Snapshots on connect |
| GUI -> POSMANAGER | Browser auto-reconnect (3s delay) | Position snapshot on connect |

### Issues Found

**ISSUE 10 (HIGH): GUIBROKER pending queue is unbounded (OOM risk)**

At `guibroker.py:77`, `_pending_queue = deque()` has no `maxlen`. If the OM is down for an extended period and the GUI continues sending orders, the queue grows without limit. Compare EXCHCONN which uses `deque(maxlen=1000)` at line 64.

**ISSUE 11 (Medium): EXCHCONN pending report queue silently drops old reports on overflow**

At `exchconn.py:64`, `_pending_reports = deque(maxlen=1000)`. If more than 1000 exec reports accumulate, the oldest are silently dropped with no logging. Dropped exec reports mean orders could be stuck in the wrong state.

**ISSUE 12 (Low): GUI does not request order state snapshot on GUIBROKER reconnect**

When the GUI reconnects to GUIBROKER, there is no mechanism to request current order state. Any exec reports missed during disconnect are lost. GUIBROKER does not implement an order state snapshot feature. Contrast with POSMANAGER, which sends position snapshots on connect.

**ISSUE 13 (Low): Proxy does not cancel peer relay task on half-close**

At `proxy.py:66-82`, the proxy uses `asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)` and does cancel pending tasks (lines 72-76). **This was fixed since the prior report.** The prior report mentioned `asyncio.gather` but the current code correctly uses `asyncio.wait` with `FIRST_COMPLETED`. Verified clean.

---

## Cross-Flow Issues

**ISSUE 14 (Medium): `_cancel_to_orig` and `_client_orders` in GUIBROKER grow without bound**

At `guibroker.py:73-75`:
- `_client_orders` (ClOrdID -> websocket): Entries are removed on GUI disconnect (`_handle_gui_disconnect`, lines 105-111), but only for the disconnecting client. Entries for still-connected clients with terminal orders persist forever.
- `_cancel_to_orig` (cancel ClOrdID -> original ClOrdID): **Never cleaned up.** Every cancel and amend adds an entry that is never removed.

Over a long-running session with many orders, these dicts grow without bound.

---

## Summary of Findings

| # | Severity | Component | Issue |
|---|----------|-----------|-------|
| 5 | **HIGH** | GUIBROKER | Amended price NOT included in execution report JSON to GUI |
| 7 | **HIGH** | OM->POSMANAGER | Failed fill sends are silently lost -- no queuing, no retry |
| 10 | **HIGH** | GUIBROKER | Pending queue is unbounded (OOM risk) |
| 3 | Medium | OM | Exec report for unknown order silently swallowed |
| 4 | Medium | OM | Cancel/amend ClOrdID alias entries leak in orders dict |
| 9 | Medium | MKTDATA | Market data sends sequential per-client (latency risk) |
| 11 | Medium | EXCHCONN | Pending report queue silently drops on overflow |
| 14 | Medium | GUIBROKER | `_cancel_to_orig` and `_client_orders` grow without bound |
| 1 | Low | GUIBROKER | `order_ack` lacks `ord_type` and `exchange` fields |
| 2 | Low | Exchange Sims | New ack exec reports lack `OrderQty` field |
| 6 | Low | Exchange Sims | Amend on limit order no immediate fill re-check |
| 8 | Low | POSMANAGER | No fill deduplication mechanism |
| 12 | Low | GUI | No order state snapshot on reconnect |

### Top 3 Priority Fixes

1. **ISSUE 5 (HIGH)**: Add `"price"` field to the execution report JSON in GUIBROKER at `guibroker.py:293-309`. Add one line: `"price": _safe_float(fix_msg.get(Tag.Price, "0"))`. This ensures the amended price from Replaced exec reports reaches the GUI.

2. **ISSUE 10 (HIGH)**: Add `maxlen` to GUIBROKER's `_pending_queue`. Change `deque()` to `deque(maxlen=1000)` at line 77, matching EXCHCONN's approach.

3. **ISSUE 7 (HIGH)**: Add a fill queue in OM for POSMANAGER sends. Create a `_pending_fills` deque, buffer fills when `pos_client` is disconnected, and flush on reconnect. Also fix `WSClient.send()` to raise or return a sentinel when `_ws is None` so callers know the message was not delivered.

### Changes Since Previous Report (2026-02-28)

**Resolved:**
1. Post-amend market order fill restart bug (previously HIGH) -- Exchange simulators now correctly pass `(order, fill_price)` to `_execute_fill()` at `binance_sim.py:443-444` and `coinbase_sim.py:432-433`.
2. GUI `order_ack` handler `ReferenceError` (previously MEDIUM) -- Now correctly uses `state.orders` Map API at `app.js:853`.
3. Proxy half-close (previously LOW) -- Now uses `asyncio.wait` with `FIRST_COMPLETED` and cancels pending tasks.
4. Cancel/amend forwarding failure rejects -- Still working correctly.
5. Stale `source_ws` after GUIBROKER reconnect -- Still working correctly.

**Carried over (still present):**
1. Missing `OrderQty` in non-Replaced exec reports (LOW -- mitigated by order_ack working now)
2. Silent fill drop to POSMANAGER (HIGH)
3. Unbounded GUIBROKER pending queue (HIGH)

**New findings:**
1. Amended price not in GUIBROKER JSON exec report (HIGH, Issue 5)
2. MKTDATA sequential per-client sends (Medium, Issue 9)
3. OM alias ClOrdID leak in orders dict (Medium, Issue 4)
4. GUIBROKER `_cancel_to_orig` unbounded growth (Medium, Issue 14)
