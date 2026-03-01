# Integration Flow Agent Report

## Mission
Trace end-to-end message flows across all component boundaries and verify nothing is lost, misrouted, or silently dropped.

## Scope
Files analyzed:
- `guibroker/guibroker.py` (416 lines)
- `om/order_manager.py` (672 lines)
- `exchconn/exchconn.py` (215 lines)
- `exchconn/binance_sim.py` (436 lines)
- `exchconn/coinbase_sim.py` (425 lines)
- `posmanager/posmanager.py` (395 lines)
- `mktdata/mktdata.py` (233 lines)
- `shared/fix_protocol.py` (258 lines)
- `shared/ws_transport.py` (192 lines)
- `shared/config.py` (133 lines)
- `shared/risk_limits.py` (79 lines)
- `gui/app.js` (1674 lines)
- `gui/server.py` (360 lines)
- `mktdata/binance_feed.py` (183 lines)
- `mktdata/coinbase_feed.py` (184 lines)

## Summary

- **Passed: 29**
- **Warnings: 8**
- **Errors: 0**
- **Info: 4**

---

## Flow 1: New Order (GUI -> GUIBROKER -> OM -> EXCHCONN -> Exchange)

### Checks

- [PASS] GUI sends JSON `new_order` with symbol, side, qty, ord_type, price, exchange
- [PASS] GUIBROKER assigns ClOrdID (GUI-N format) and tracks GUI websocket in `_client_orders`
- [PASS] GUIBROKER converts JSON to FIX `NewOrderSingle` via `new_order_single()` factory
- [PASS] GUIBROKER sends `order_ack` JSON back to GUI with assigned ClOrdID
- [PASS] GUIBROKER queues messages in `_pending_queue` if OM is disconnected
- [PASS] OM receives FIX NOS, validates symbol, qty, price, exchange, and risk limits
- [PASS] OM assigns OM-ID (OM-NNNNNN), stores order in internal book, creates reverse mapping
- [PASS] OM adds OrderID and resolved ExDestination to FIX message before forwarding
- [PASS] OM rejects to GUIBROKER with FIX ExecutionReport(Rejected) if validation fails
- [PASS] OM sends FIX reject back if EXCHCONN send fails
- [PASS] EXCHCONN parses FIX, resolves exchange from ExDestination or DEFAULT_ROUTING
- [PASS] EXCHCONN performs defense-in-depth qty sanity checks (>0, <1000)
- [PASS] Exchange simulator assigns exchange-specific order ID (BIN-N / CB-N), stores order state

### Issues

- [WARNING] **GUI ignores `order_ack` message type** — `gui/app.js` line 809-814: `onBrokerMessage()` only handles `execution_report`, silently dropping `order_ack`. The order's original qty, price, and symbol are never stored from the ack. The order only appears in the blotter when the first execution report arrives, at which point `qty` and `price` fields are missing (see Flow 2 issues below).
  - **File:** `gui/app.js` line 809-814
  - **Recommendation:** Handle `order_ack` in `onBrokerMessage()` to pre-populate the order in the blotter with correct qty and price before the first exec report arrives.

---

## Flow 2: Execution Report (Exchange -> EXCHCONN -> OM -> GUIBROKER -> GUI)

### Checks

- [PASS] Exchange simulators send exec report via `_report_callback` (set by EXCHCONN)
- [PASS] Exchange simulators set `Tag.ExDestination` on all reports
- [PASS] EXCHCONN broadcasts exec reports to all connected OM clients
- [PASS] OM looks up order by ClOrdID, falling back to reverse lookup by OrderID
- [PASS] OM updates internal order book (status, cum_qty, leaves_qty, avg_px)
- [PASS] OM forwards FIX exec report to originating GUIBROKER websocket
- [PASS] GUIBROKER converts FIX exec report to JSON with human-readable enums
- [PASS] GUIBROKER routes to correct GUI client via `_client_orders` mapping, or broadcasts if mapping missing

### Issues

- [WARNING] **Missing OrderQty in execution reports causes GUI display bug** — The `execution_report()` factory in `shared/fix_protocol.py` (line 192-223) does not set `Tag.OrderQty`. GUIBROKER reads `fix_msg.get(Tag.OrderQty, "0")` at line 277 and sends `"qty": "0"` to the GUI. Combined with the ignored `order_ack`, the GUI blotter always shows qty=0 for all orders. The fill percentage calculation at `gui/app.js` line 440-444 also breaks since `o.qty` is `"0"`.
  - **File:** `guibroker/guibroker.py` line 277, `shared/fix_protocol.py` line 192-223
  - **Recommendation:** Either (a) have `execution_report()` accept and set `OrderQty`, or (b) have the OM enrich the exec report with `Tag.OrderQty` from its order book before forwarding, or (c) have GUIBROKER look up the original order qty from its own records.

- [WARNING] **Missing exchange field in execution report JSON** — GUIBROKER's `_handle_execution_report` (line 269-283) does not extract `Tag.ExDestination` from the FIX exec report. The JSON sent to GUI lacks an `exchange` field, so the exchange column in the GUI blotter is always empty.
  - **File:** `guibroker/guibroker.py` line 269-283
  - **Recommendation:** Add `"exchange": fix_msg.get(Tag.ExDestination, "")` to the JSON report.

- [WARNING] **String vs number type mismatch in exec report fields** — All values extracted from FIX messages via `fix_msg.get()` are strings (e.g., `"1.5"` instead of `1.5`). The JSON sent to the GUI has string-typed numeric fields (`qty`, `filled_qty`, `avg_px`, `leaves_qty`, `last_px`, `last_qty`). The GUI relies on JavaScript type coercion for comparisons and arithmetic, which works but is fragile and could produce subtle bugs.
  - **File:** `guibroker/guibroker.py` line 277-282
  - **Recommendation:** Convert numeric FIX fields to `float()` before JSON serialization.

- [INFO] **Exec reports for unknown orders are silently dropped** — OM at line 512-518 logs a warning but does not forward any response when it receives an exec report for an order it doesn't know about. This is acceptable behavior but means the exec report is permanently lost.
  - **File:** `om/order_manager.py` line 512-518

---

## Flow 3: Cancel (GUI -> GUIBROKER -> OM -> EXCHCONN -> Exchange -> response back)

### Checks

- [PASS] GUI sends JSON `cancel_order` with `cl_ord_id`, `symbol`, `side`
- [PASS] GUIBROKER assigns new ClOrdID for cancel, stores `_cancel_to_orig` mapping
- [PASS] GUIBROKER tracks cancel ClOrdID in `_client_orders` for response routing
- [PASS] OM validates original order exists in internal book
- [PASS] OM rejects cancel for unknown orders with exec report
- [PASS] OM maps cancel ClOrdID to same order dict for exec report lookup
- [PASS] OM adds OrderID to cancel request before forwarding
- [PASS] Exchange simulator looks up order by OrigClOrdID, cancels fill task, sends Canceled report
- [PASS] Exchange rejects cancel for unknown or inactive orders
- [PASS] GUIBROKER maps cancel response back to original ClOrdID for GUI

### Issues

- [WARNING] **Cancel forwarding failure silently swallowed** — When OM fails to send the cancel request to EXCHCONN (line 287-288), it only logs an error. No reject exec report is sent back to GUIBROKER/GUI. The GUI remains in a state where the cancel was requested but neither confirmed nor rejected.
  - **File:** `om/order_manager.py` line 285-288
  - **Recommendation:** Send a Rejected exec report back to GUIBROKER on cancel forwarding failure, analogous to the new order path (line 231-249).

---

## Flow 4: Amend (GUI -> GUIBROKER -> OM -> EXCHCONN -> Exchange -> response back)

### Checks

- [PASS] GUI sends JSON `amend_order` with `cl_ord_id`, `symbol`, `side`, `qty`, `price`
- [PASS] GUIBROKER assigns new ClOrdID, stores mapping in `_cancel_to_orig` and `_client_orders`
- [PASS] OM validates original order exists, checks new qty > 0, new price > 0 for limit orders
- [PASS] OM performs risk checks on amended values (max_order_qty, max_notional, max_position_qty)
- [PASS] OM rejects invalid amend requests with exec report
- [PASS] Exchange simulator updates order qty/price, updates cl_ord_id mapping, sends Replaced report
- [PASS] GUIBROKER maps Replaced response back to original ClOrdID for GUI

### Issues

- [WARNING] **Amend forwarding failure silently swallowed** — Same issue as cancel: OM at line 439-441 only logs an error when it fails to forward the amend to EXCHCONN. No reject is sent back to GUIBROKER/GUI.
  - **File:** `om/order_manager.py` line 438-441
  - **Recommendation:** Send a Rejected exec report back to GUIBROKER on amend forwarding failure.

- [WARNING] **OM order book not updated with amended qty/price from Replaced report** — The exchange's Replaced exec report (constructed by `execution_report()` factory) does not include `Tag.OrderQty` or `Tag.Price`. When the OM processes the Replaced report at line 571-584, it reads `fix_msg.get(Tag.OrderQty, str(order["qty"]))` and `fix_msg.get(Tag.Price, str(order["price"]))`, which fall back to the original order values. The OM never learns the amended values from the Replaced report.
  Meanwhile, the OM does NOT update the order dict's qty/price when it receives the amend request (line 290-441 only validates and forwards). This creates a **state divergence**: the exchange simulator has the amended qty/price, but the OM's order book retains the original values.
  Consequences: (a) leaves_qty at line 580 is computed from the old qty, (b) subsequent risk checks reference old qty, (c) the GUI receives old qty in any future exec reports.
  - **File:** `om/order_manager.py` line 571-584, `exchconn/binance_sim.py` line 424-434
  - **Recommendation:** Either (a) have the exchange simulator include `Tag.OrderQty` and `Tag.Price` in the Replaced exec report, or (b) have the OM store the pending amend values and apply them when the Replaced report arrives.

---

## Flow 5: Fill Notification (OM -> POSMANAGER)

### Checks

- [PASS] OM sends JSON fill notification to POSMANAGER on every Trade exec type
- [PASS] Fill notification includes symbol, side (BUY/SELL string), qty, price, cl_ord_id, order_id
- [PASS] OM updates internal position tracking (`_positions` dict) under lock
- [PASS] POSMANAGER validates fill data (non-empty symbol, valid side, qty > 0, price > 0)
- [PASS] POSMANAGER applies fill to Position object with correct long/short/crossing logic
- [PASS] POSMANAGER schedules throttled broadcast after fill

### Issues

- [INFO] **Fill notifications lost during POSMANAGER disconnection** — OM sends fills via `pos_client.send()` at line 562. If POSMANAGER is disconnected, `WSClient.send()` silently does nothing (line 129-130 of ws_transport.py: `if self._ws: await self._ws.send(message)`). There is no queuing or retry. Fills during disconnection are permanently lost, causing position divergence between OM and POSMANAGER.
  - **File:** `om/order_manager.py` line 561-565, `shared/ws_transport.py` line 127-130
  - **Recommendation:** Add a fill queue in OM that persists fills and replays them on POSMANAGER reconnection. Alternatively, add a position reconciliation mechanism.

---

## Flow 6: Market Data (MKTDATA -> POSMANAGER -> GUI)

### Checks

- [PASS] MKTDATA generates simulated ticks per symbol from BinanceFeed and CoinbaseFeed
- [PASS] MKTDATA caches latest tick per (symbol, exchange) and sends snapshots on client connect
- [PASS] MKTDATA broadcasts ticks to subscribed clients
- [PASS] POSMANAGER connects to MKTDATA as WS client with auto-reconnect
- [PASS] POSMANAGER updates Position.market_price from `last` field (or bid/ask midpoint fallback)
- [PASS] POSMANAGER recalculates unrealized P&L and broadcasts position updates (throttled 2/sec)

### Issues

- [INFO] **Cross-exchange price overwriting for same symbol** — POSMANAGER at line 262-267 updates `market_price` for a symbol regardless of which exchange the tick came from. BTC/USD prices from BINANCE and COINBASE (which differ slightly due to independent simulators) overwrite each other on every tick. This causes the unrealized P&L display to jitter between exchange prices.
  - **File:** `posmanager/posmanager.py` line 262-267
  - **Recommendation:** Either aggregate prices across exchanges (e.g., use the last price from the exchange the position was filled on), or average bid/ask across exchanges for a more stable valuation.

---

## Flow 7: Disconnect/Reconnect

### Checks

- [PASS] GUIBROKER queues messages in `_pending_queue` when OM is disconnected
- [PASS] GUIBROKER flushes pending queue on OM reconnection with error handling
- [PASS] WSClient auto-reconnects with configurable delay (2s)
- [PASS] MKTDATA sends latest snapshots to newly connected clients
- [PASS] POSMANAGER sends current positions to newly connected clients
- [PASS] GUIBROKER cleans up stale order mappings when GUI client disconnects

### Issues

- [WARNING] **Stale source_ws reference after GUIBROKER reconnect causes lost exec reports** — When GUIBROKER disconnects from OM and reconnects, all existing orders in OM's order book still hold a reference to the old (closed) websocket in `order["source_ws"]` (line 212). When fills arrive for these orders, OM tries `server.send_to(source_ws, fwd_json)` at line 603, which fails because the websocket is closed. The error is caught but the exec report is lost -- it is not retried or broadcast to the new GUIBROKER connection.
  The OM's `_handle_guibroker_disconnect` at line 140-141 does nothing to update or invalidate stale `source_ws` references.
  - **File:** `om/order_manager.py` line 140-141, 598-608
  - **Recommendation:** On GUIBROKER reconnect, update `source_ws` for all open orders to the new websocket. Or on send failure, fall back to broadcasting to all connected GUIBROKER clients.

- [WARNING] **EXCHCONN exec reports lost during OM disconnection** — EXCHCONN's `_on_execution_report` at line 126-136 iterates over `_om_clients` and sends to each. If OM is disconnected, the set is empty and the report is silently dropped. There is no message queue or retry. Exchange fills that occur while OM is disconnected are permanently lost.
  - **File:** `exchconn/exchconn.py` line 126-136
  - **Recommendation:** Add a bounded queue in EXCHCONN for exec reports that fail to send, and replay them when an OM client reconnects.

- [INFO] **No bounded queue size or TTL for GUIBROKER pending queue** — GUIBROKER's `_pending_queue` (deque) has no maximum size or time-to-live. During extended OM outages, the queue grows unboundedly. Stale cancel/amend requests may be sent after reconnection for orders that have already been filled or timed out at the exchange.
  - **File:** `guibroker/guibroker.py` line 77
  - **Recommendation:** Add a `maxlen` to the deque and/or discard messages older than a configurable TTL.

---

## Final Tally

| Severity | Count |
|----------|-------|
| PASS     | 29    |
| WARNING  | 8     |
| INFO     | 4     |
| ERROR    | 0     |

### Critical Path Summary

The core happy-path flows (new order, fill, cancel, amend, market data) all work end-to-end with correct identifier mapping and protocol translation. The system handles basic error cases (unknown orders, invalid fields, exchange rejects) with proper reject propagation.

The most impactful issues are:

1. **Amend state divergence** (WARNING) -- After a successful amend, OM's order book retains the old qty/price because neither the OM amend handler stores the pending values nor the exchange's Replaced report carries them. This affects subsequent risk checks and GUI display.

2. **Lost exec reports on reconnection** (WARNING) -- GUIBROKER reconnect leaves stale `source_ws` references in OM, causing all exec reports for pre-existing orders to be silently dropped. EXCHCONN also drops exec reports when OM is disconnected. Together, these create a gap where fills can be permanently lost during connectivity interruptions.

3. **GUI display bugs from missing fields** (WARNING) -- The execution report JSON lacks `OrderQty` (shows 0), `Price` (shows MKT for all), and `exchange` (shows empty), making the order blotter unreliable. The root cause is the `execution_report()` factory function not including these fields, combined with the GUI ignoring the `order_ack` message.
