# Crypto Trading System - Architecture Documentation

## System Overview

A distributed, asynchronous crypto trading platform built with Python. Six independent components communicate over WebSocket using FIX protocol (for order flow) and JSON (for market data and positions). The system supports simulated and real (Coinbase) exchange connectivity.

```
                                    EXCHANGES
                                 +-----------+
                                 | BINANCE   |
GUI ──JSON──> GUIBROKER ──FIX──> OM ──FIX──> EXCHCONN ──REST/WS──>|           |
 ^                                |           |           | COINBASE  |
 |                                |           +-----------+-----------+
 |                                | fills (JSON)
 |                                v
 +──JSON──────────────────── POSMANAGER <──JSON── MKTDATA <── Exchange Feeds
 +──JSON──────────────────── MKTDATA
```

---

## Components

### 1. GUI (port 8080) - Web Frontend

**Purpose:** HTTP server + static single-page application for the trading terminal.

**Technology:** Python `http.server` serving HTML/CSS/JS.

**Serves:**
- `index.html` - Trading terminal UI (market data, order entry, blotter, positions)
- `app.js` - Frontend logic (WebSocket connections, rendering, modals)
- `styles.css` - Dark-themed styling

**API Endpoints:**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/config` | GET | System configuration snapshot |
| `/api/status` | GET | Component health (probes ports 8080-8085) |
| `/api/risk-limits` | GET | Current risk limit settings |
| `/api/risk-limits` | POST | Save updated risk limits |

**Connects to (as WS client from browser JS):**
- MKTDATA (8081) - real-time price updates
- GUIBROKER (8082) - order submission and execution reports
- POSMANAGER (8085) - position and P&L updates

---

### 2. MKTDATA (port 8081) - Market Data Feed

**Purpose:** Aggregates market data from exchange feeds and broadcasts to subscribers.

**Protocol:** WebSocket + JSON

**Role:** Server only. Clients connect to receive market data.

**Feeds:**
- **BinanceFeed** (simulator) - random walk with mean reversion, 0.01% spread, ticks every 0.5-1.5s
- **CoinbaseFeed** (simulator) or **CoinbaseLiveFeed** (real) - 0.012% spread

**Message Format (outbound):**
```json
{
  "type": "market_data",
  "symbol": "BTC/USD",
  "bid": 67450.50,
  "ask": 67456.25,
  "last": 67453.00,
  "bid_size": 1.5,
  "ask_size": 0.8,
  "volume": 12345.67,
  "exchange": "BINANCE"
}
```

**Client Messages:**
- `subscribe` - subscribe to specific symbols (default: all)
- `unsubscribe` - remove symbols from subscription

**State:** Per-client subscription sets, cached latest tick per (symbol, exchange).

**Consumers:** GUI (price display), POSMANAGER (mark-to-market pricing).

---

### 3. GUIBROKER (port 8082) - Order Gateway

**Purpose:** Protocol bridge translating between GUI JSON and OM FIX messages.

**Protocol:**
- Server side: WebSocket + JSON (GUI clients connect here)
- Client side: WebSocket + FIX (connects to OM on port 8083)

**Key Responsibilities:**
- Assigns `ClOrdID` to each order (e.g., `GUI-1`, `GUI-2`, ...)
- Translates JSON order messages to FIX `NewOrderSingle`, `OrderCancelRequest`, `OrderCancelReplaceRequest`
- Translates FIX `ExecutionReport` back to JSON for the GUI
- Routes execution reports to the originating GUI client
- Queues orders if OM is temporarily disconnected

**JSON to FIX Translation:**

| GUI JSON `type` | FIX MsgType |
|-----------------|-------------|
| `new_order` | NewOrderSingle (D) |
| `cancel_order` | OrderCancelRequest (F) |
| `amend_order` | OrderCancelReplaceRequest (G) |

**Cancel/Amend Mapping:**
When a cancel or amend is sent, GUIBROKER generates a new `ClOrdID` and stores a mapping back to the original order's `ClOrdID`. When the execution report returns, it maps it back so the GUI updates the correct blotter row.

**State:** `_client_orders` (ClOrdID to websocket), `_cancel_to_orig` (cancel/amend ID to original ID), `_pending_queue` (for offline OM).

---

### 4. OM - Order Manager (port 8083) - Central Routing Engine

**Purpose:** Central order routing, validation, risk checking, and state management.

**Protocol:** WebSocket + FIX

**Connections:**
- **Server (port 8083):** GUIBROKER connects here to send orders
- **Client to EXCHCONN (8084):** Sends validated orders for execution
- **Client to POSMANAGER (8085):** Sends fill notifications

**Key Responsibilities:**
1. Receive FIX orders from GUIBROKER
2. Validate fields (symbol, quantity, price)
3. Apply pre-trade risk checks (see Risk Controls below)
4. Assign OM order ID (`OM-000001`, etc.)
5. Resolve target exchange (explicit or via default routing)
6. Forward to EXCHCONN
7. Process execution reports from EXCHCONN
8. Track positions from fills
9. Forward execution reports back to GUIBROKER
10. Send fill notifications to POSMANAGER

**FIX Messages Handled:**

| Inbound (from GUIBROKER) | Outbound |
|--------------------------|----------|
| NewOrderSingle (D) | Forward to EXCHCONN or Reject |
| OrderCancelRequest (F) | Forward to EXCHCONN or Reject |
| OrderCancelReplaceRequest (G) | Forward to EXCHCONN or Reject |
| OrderStatusRequest (H) | Reply with current status |

| Inbound (from EXCHCONN) | Action |
|--------------------------|--------|
| ExecutionReport (8) - New | Update status, forward to GUIBROKER |
| ExecutionReport (8) - Trade | Update fills, track position, notify POSMANAGER, forward |
| ExecutionReport (8) - Canceled | Update status, forward |
| ExecutionReport (8) - Replaced | Update qty/price, forward |
| ExecutionReport (8) - Rejected | Update status, forward |

**State:**
- `orders` - internal order book (`cl_ord_id` -> order dict with status, fills, etc.)
- `_om_id_to_cl_ord_id` - reverse lookup by OM-assigned order ID
- `_positions` - net position per symbol tracked from fills (for risk checks)

---

### 5. EXCHCONN (port 8084) - Exchange Connector

**Purpose:** Routes orders to the appropriate exchange simulator and forwards execution reports back.

**Protocol:** WebSocket + FIX

**Role:** Server. OM connects as a client.

**Exchange Routing:**
- Uses `ExDestination` tag from the FIX message
- Falls back to `DEFAULT_ROUTING` by symbol
- Routes to BinanceSimulator or CoinbaseSimulator (or CoinbaseAdapter for real trading)

**Exchange Simulators:**
- **BinanceSimulator** - Order ID prefix `BIN-`, 100-2000ms processing delay, 1-3 partial fills, 0.1% price jitter
- **CoinbaseSimulator** - Order ID prefix `CB-`, 100-2000ms delay, 1-3 partial fills, 0.12% price jitter

**Execution Report Generation:**
1. `ExecType.New` - Order acknowledged
2. `ExecType.Trade` - Partial or full fill with `LastQty`, `LastPx`
3. `ExecType.Canceled` - Order canceled
4. `ExecType.Replaced` - Order amended with new qty/price
5. `ExecType.Rejected` - Order rejected by exchange

---

### 6. POSMANAGER (port 8085) - Position Tracker

**Purpose:** Real-time position and P&L tracking.

**Protocol:** WebSocket + JSON

**Connections:**
- **Server (port 8085):** OM sends fills, GUI subscribes for updates
- **Client to MKTDATA (8081):** Receives market prices for mark-to-market

**Position Tracking:**
Each symbol has a `Position` object:
- `qty` - net quantity (+long, -short)
- `avg_cost` - weighted average entry price
- `market_price` - latest from MKTDATA
- `unrealized_pnl` - `(market_price - avg_cost) * qty`
- `realized_pnl` - accumulated from closed trades

**Fill Processing:**
- BUY while flat/long: adds to position, updates weighted avg cost
- SELL while flat/short: adds to short, updates weighted avg cost
- BUY to close short: realizes P&L = `(avg_cost - fill_price) * close_qty`
- SELL to close long: realizes P&L = `(fill_price - avg_cost) * close_qty`

**Broadcast:** Throttled to max 2/sec to prevent GUI flooding. Sends `position_update` with all positions.

---

## Shared Modules

### `shared/config.py`
System-wide configuration: ports, symbols, exchanges, routing defaults, risk limit defaults.

### `shared/fix_protocol.py`
FIX 4.4 message implementation. `FIXMessage` class with tag-value pairs, factory functions for common message types (`new_order_single`, `execution_report`, `cancel_request`, `cancel_replace_request`).

### `shared/ws_transport.py`
WebSocket transport layer. `WSServer` (async server with broadcast/send_to), `WSClient` (auto-reconnecting client), `json_msg` helper.

### `shared/risk_limits.py`
Risk limit persistence and checking. `load_limits()`, `save_limits()`, `check_order()`.

### `shared/logging_config.py`
Per-component logging setup with `log_recv`/`log_send` helpers.

---

## Message Flows

### New Order Lifecycle

```
GUI                GUIBROKER           OM                EXCHCONN         Exchange
 |                    |                 |                    |                |
 |-- new_order JSON ->|                 |                    |                |
 |                    |-- FIX D ------->|                    |                |
 |                    |                 |-- validate ------->|                |
 |                    |                 |-- risk check ----->|                |
 |                    |                 |-- assign OM ID     |                |
 |                    |                 |-- FIX D ---------->|                |
 |                    |                 |                    |-- submit ----->|
 |                    |                 |                    |                |
 |                    |                 |                    |<-- ack --------|
 |                    |                 |<-- FIX 8 (New) ----|                |
 |                    |<-- FIX 8 (New) -|                    |                |
 |<-- exec_report ----|                 |                    |                |
 |                    |                 |                    |<-- fill -------|
 |                    |                 |<-- FIX 8 (Trade) --|                |
 |                    |                 |-- update position  |                |
 |                    |                 |-- fill to POSMANAGER                |
 |                    |<-- FIX 8 (Trade)|                    |                |
 |<-- exec_report ----|                 |                    |                |
```

### Cancel Flow

```
GUI -> GUIBROKER: {"type": "cancel_order", "cl_ord_id": "GUI-1"}
GUIBROKER -> OM:  FIX OrderCancelRequest (new ClOrdID, OrigClOrdID=GUI-1)
OM -> EXCHCONN:   FIX OrderCancelRequest (with OrderID)
EXCHCONN -> Exchange: cancel_order()
Exchange -> EXCHCONN: FIX ExecutionReport (ExecType=Canceled)
EXCHCONN -> OM:   forward report
OM -> GUIBROKER:  forward report (updates order status, leaves_qty=0)
GUIBROKER -> GUI: JSON execution_report (mapped back to original cl_ord_id)
```

### Amend Flow

```
GUI -> GUIBROKER: {"type": "amend_order", "cl_ord_id": "GUI-1", "qty": 2, "price": 68000}
GUIBROKER -> OM:  FIX OrderCancelReplaceRequest (new ClOrdID, OrigClOrdID=GUI-1)
OM:               validate new qty/price + risk checks
OM -> EXCHCONN:   FIX OrderCancelReplaceRequest (if valid)
EXCHCONN -> Exchange: amend_order()
Exchange -> EXCHCONN: FIX ExecutionReport (ExecType=Replaced)
EXCHCONN -> OM:   forward report
OM:               update order qty, price, leaves_qty
OM -> GUIBROKER:  forward report
GUIBROKER -> GUI: JSON execution_report (mapped back to original cl_ord_id)
```

### Market Data Flow

```
Exchange Feeds -> MKTDATA: generate ticks (0.5-1.5s intervals)
MKTDATA -> GUI:        broadcast market_data JSON
MKTDATA -> POSMANAGER: broadcast market_data JSON
POSMANAGER:            update Position.market_price, recalculate unrealized P&L
POSMANAGER -> GUI:     broadcast position_update JSON (throttled 2/sec)
```

---

## Risk Controls

### Overview

Risk limits are stored in `risk_limits.json` at the project root and can be edited live from the GUI (RISK LIMITS button) without restarting the system. The OM re-reads the file on every order check.

### Default Limits

| Limit | Scope | Default Values |
|-------|-------|---------------|
| Max Order Qty | Per-symbol | BTC: 10, ETH: 100, SOL: 5,000, ADA: 100,000, DOGE: 500,000 |
| Max Order Notional | Global | $100,000 (limit orders only) |
| Max Position Size | Per-symbol | BTC: 50, ETH: 500, SOL: 25,000, ADA: 500,000, DOGE: 2,500,000 |
| Max Open Orders | Global | 50 |

### Where Risk Checks Are Applied

**1. New Orders (`OM._validate_order`)**

All 4 checks are applied in sequence after basic field validation:

| # | Check | Condition | Reject Message |
|---|-------|-----------|----------------|
| 1 | Max Order Qty | `qty > max_order_qty[symbol]` | "Order qty X exceeds max Y for SYMBOL" |
| 2 | Max Order Notional | `qty * price > max_order_notional` (limit orders only) | "Order notional $X exceeds max $Y" |
| 3 | Max Position Size | `abs(current_pos + signed_qty) > max_position_qty[symbol]` | "Projected position X exceeds max Y for SYMBOL" |
| 4 | Max Open Orders | `open_count >= max_open_orders` | "Open order count X has reached max Y" |

Open orders are counted as orders with status `New`, `PendingNew`, or `PartiallyFilled`.

Position check uses the OM's internal position tracker (`_positions`), which is updated from every fill.

**2. Amend Requests (`OM._handle_cancel_replace_request`)**

After basic qty/price validation, two checks are applied to the amended values:

| # | Check | Condition |
|---|-------|-----------|
| 1 | Max Order Qty | `new_qty > max_order_qty[symbol]` |
| 2 | Max Order Notional | `new_qty * new_price > max_order_notional` (limit orders only) |

**3. Cancel Requests**

No risk checks. Only validates the order exists.

### Rejection Behavior

When a risk check fails:
1. OM generates a FIX `ExecutionReport` with `ExecType=Rejected`, `OrdStatus=Rejected`
2. The reject reason is set in the `Text` tag
3. The report is sent back to GUIBROKER immediately (order never reaches EXCHCONN)
4. GUIBROKER converts to JSON and sends to GUI
5. GUI displays the order as `REJECTED` in the blotter

### Position Tracking for Risk

The OM tracks positions independently from POSMANAGER by accumulating signed fill quantities:

```
On each fill (ExecType.Trade):
    signed_qty = +last_qty if BUY, -last_qty if SELL
    positions[symbol] += signed_qty
```

This gives the OM a local position view for the max position check without querying POSMANAGER.

### Editing Limits at Runtime

1. Click **RISK LIMITS** button in the GUI header
2. Edit values in the modal form (per-symbol qty/position limits, global notional/open orders)
3. Click **Save** - writes to `risk_limits.json`
4. Next order check reads the updated file automatically

---

## Configuration

### Symbols
`BTC/USD`, `ETH/USD`, `SOL/USD`, `ADA/USD`, `DOGE/USD`

### Exchanges & Symbol Mapping

| Internal | BINANCE | COINBASE |
|----------|---------|----------|
| BTC/USD | BTCUSDT | BTC-USD |
| ETH/USD | ETHUSDT | ETH-USD |
| SOL/USD | SOLUSDT | SOL-USD |
| ADA/USD | ADAUSDT | ADA-USD |
| DOGE/USD | DOGEUSDT | DOGE-USD |

### Default Routing

| Symbol | Exchange |
|--------|----------|
| BTC/USD | BINANCE |
| ETH/USD | BINANCE |
| SOL/USD | COINBASE |
| ADA/USD | BINANCE |
| DOGE/USD | COINBASE |

### Ports

| Component | Port |
|-----------|------|
| GUI | 8080 |
| MKTDATA | 8081 |
| GUIBROKER | 8082 |
| OM | 8083 |
| EXCHCONN | 8084 |
| POSMANAGER | 8085 |

### Startup

```bash
python run_all.py          # Start all components
python restart.py          # Kill all + restart
python restart.py --no-gui # Backend only
```
