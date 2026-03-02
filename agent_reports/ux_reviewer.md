# UX Reviewer Agent Report
## Date: 2026-03-01
## Summary
The trading UI is a solid, professional dark-themed terminal with well-implemented core workflows. Previous critical bugs (order_ack handler crash, Ctrl+Enter crash) have been fixed. Remaining issues are informational gaps (no high/low, no position weights, no price change delta column) and usability refinements (blotter sorting/filtering, toast dismissal, keyboard shortcut discoverability) that would bring it closer to institutional-grade terminals.

## Findings

### [MEDIUM] No position weight or allocation percentage columns
- **Pillar**: Informative
- **Location**: gui/index.html:170-181, gui/app.js:665-701
- **Issue**: The positions table shows Symbol, Direction, Qty, Avg Cost, Mkt Price, Unrealized PnL, and Realized PnL, but lacks a "Weight %" column showing what percentage of the total portfolio each position represents by notional value.
- **Impact**: A trader managing a multi-asset portfolio cannot quickly assess concentration risk. They must mentally calculate the notional value of each position and compare it to total equity -- a routine task that professional terminals automate.
- **Suggested Fix**: Add a "Weight" column computed as `abs(qty * mkt_price) / total_portfolio_notional * 100` for each position and display it as a percentage.

### [MEDIUM] No high/low or 24-hour range display in market data
- **Pillar**: Informative
- **Location**: gui/index.html:36-48, gui/app.js:144-160
- **Issue**: The market data table shows Bid, Ask, Spread, Last, Mid, and Volume, but does not display 24-hour High, Low, or price change delta. The `onMarketData` handler does not capture or display high/low fields even if the backend provides them.
- **Impact**: Traders cannot assess intraday range or directional momentum. They must consult an external chart to understand where the current price sits relative to the day's extremes, which is basic context that every professional terminal provides.
- **Suggested Fix**: Add "High", "Low", and "Chg" columns to the market data table. If the backend does not provide these fields, track session high/low from observed `last` prices locally.

### [MEDIUM] No persistent price change direction indicator
- **Pillar**: Informative
- **Location**: gui/app.js:174-229
- **Issue**: Price flash animations (flash-green/flash-red) briefly indicate up/down ticks for 400ms, but there is no persistent price change indicator (e.g., an arrow, +/- delta column, or color-coded change value). Once the animation ends, there is no visual cue about recent price direction.
- **Impact**: A trader who glances at the screen after the flash has ended cannot tell whether prices have been trending up or down. Professional terminals show a persistent change column with color-coded deltas.
- **Suggested Fix**: Add a "Chg" column that computes the delta from the previous last price, displays it with + or - prefix, and color-codes it green/red. The value persists until the next tick overwrites it.

### [MEDIUM] No sorting or filtering in the order blotter
- **Pillar**: Functional
- **Location**: gui/app.js:425-476, gui/index.html:127-144
- **Issue**: The blotter renders orders in reverse insertion order (newest first) with no ability to sort by column (symbol, status, side) or filter by status (active only, filled only). The table headers are static elements with no click handlers. The Records modal has filtering, but the main blotter does not.
- **Impact**: When a trader has accumulated many orders across multiple symbols, finding a specific order or viewing only active orders requires visually scanning the entire list. This becomes unworkable beyond approximately 20-30 orders.
- **Suggested Fix**: Add clickable column headers for sort toggling and a row of filter buttons (All / Active / Filled / Canceled) above the blotter.

### [LOW] Symbol dropdown change does not auto-populate price field
- **Pillar**: Intuitive
- **Location**: gui/app.js:298-301
- **Issue**: When the user changes the symbol via the dropdown selector, the `change` event listener calls only `updateSpreadDisplay()` and `updateNotionalDisplay()`. It does not auto-populate the price field from market data or highlight the corresponding row in the market data table. The price auto-fill only occurs when clicking a row in the market data table (via `selectSymbol`).
- **Impact**: A trader who uses the dropdown instead of clicking the market data row will have to manually type the price, creating inconsistent behavior between two paths to the same action (selecting a symbol).
- **Suggested Fix**: In the `oeSymbol` change handler, call `selectSymbol(dom.oeSymbol.value)` to get the full behavior including price population and row highlighting.

### [LOW] Order form does not clear price field after submission
- **Pillar**: Simple
- **Location**: gui/app.js:367-369
- **Issue**: After submitting an order, only the quantity field is cleared (`dom.oeQty.value = ""`). The price field retains its previous value. If a trader switches symbols after submitting, the stale price from the previous symbol remains.
- **Impact**: Minor -- a trader could accidentally submit an order at a stale price if they switch symbols without updating the price. The risk is partially mitigated by the fact that clicking a market data row updates the price, but the dropdown path (see finding above) does not.
- **Suggested Fix**: Clear the price field on symbol change, or update it to the new symbol's bid/ask. Alternatively, clear it on submission if symbol is also cleared, but keeping the symbol is valid for order laddering.

### [LOW] Toast notifications have no close button and fixed 3-second timeout
- **Pillar**: Simple
- **Location**: gui/app.js:937-951
- **Issue**: Toast notifications auto-dismiss after 3 seconds with no manual dismiss option (no X button, no click-to-close). For critical notifications like order rejects, 3 seconds may not be enough time for a trader to read and understand the error, especially if they were looking elsewhere.
- **Impact**: A trader who receives a rejection toast while focused on another part of the screen may miss it entirely. There is no notification history or way to retrieve dismissed messages outside of the Records modal.
- **Suggested Fix**: Add a click-to-dismiss handler on toasts. Increase timeout to 5-6 seconds for error-type toasts. Consider keeping error toasts until manually dismissed.

### [LOW] No visible keyboard shortcut documentation
- **Pillar**: Intuitive
- **Location**: gui/app.js:1665-1691
- **Issue**: Keyboard shortcuts exist (Ctrl+B for BUY, Ctrl+S for SELL, Ctrl+Enter for submit, Escape to close modals) but are not documented anywhere in the UI. There are no tooltips on buttons, no help modal, and no "?" shortcut to show a cheat sheet.
- **Impact**: Traders who would benefit most from keyboard shortcuts (experienced, speed-oriented traders) will not discover them without reading the source code.
- **Suggested Fix**: Add a "?" button in the header that opens a keyboard shortcut reference card, or add `title` attributes to the BUY/SELL buttons showing their shortcuts (e.g., `title="Ctrl+B"`).

## Previously Fixed Issues
The following issues were identified in a prior review and have since been resolved:

- **order_ack handler** (gui/app.js:850-869): Now correctly uses `state.orders.has(id)` and `state.orders.set(id, ...)` Map API calls instead of bracket notation on an undefined `orders` variable.
- **Ctrl+Enter keyboard shortcut** (gui/app.js:329-330, 1688): `submitOrder()` now guards `e.preventDefault()` with `if (e && e.preventDefault)`, so the keyboard shortcut path (which passes no event) works correctly.
- **order_ack silent drop** (Learned Theme): `onBrokerMessage()` now explicitly handles `order_ack` messages and populates the blotter at PENDING_NEW stage.

## No Issues Found
The following areas were evaluated and found to meet professional trading terminal standards:

- **Dark theme implementation** (Simple): Well-executed color variable system with proper contrast ratios for dark-background trading aesthetics.
- **BUY/SELL visual distinction** (Intuitive): Green/red color coding on side toggle, submit button, and blotter rows is clear and consistent.
- **Submit button reflects selected side** (Intuitive): Button text and color change correctly between BUY and SELL.
- **Price field disabled for market orders** (Intuitive): `onOrdTypeChange` correctly disables/enables the price field.
- **Click-to-trade from market data** (Intuitive): Clicking a row auto-populates symbol and price in the order form.
- **Double-click to amend** (Intuitive): Active order rows have `ondblclick` handlers that open the amend modal.
- **Position direction labels** (Intuitive): LONG/SHORT/FLAT with green/red/grey color coding.
- **Monospace fonts for numerical data** (Simple): All data tables and inputs use `var(--font-mono)`.
- **Fixed viewport layout, no scrolling** (Simple): Single-screen layout with `overflow: hidden`.
- **WebSocket auto-reconnection** (Functional): 3-second reconnect delay with automatic retry.
- **Toast notifications for events** (Functional): Fills, rejects, and connection errors all trigger toasts.
- **Cancel/Amend buttons only on active orders** (Functional): Buttons appear only for NEW, PARTIALLY_FILLED, PENDING_NEW states.
- **Amend modal pre-fills current values** (Functional): Modal correctly populates with current order qty and price.
- **Risk limits editable via GUI** (Functional): Full CRUD modal via REST API, no restart required.
- **System status with architecture diagram** (Functional): Component health visualization with up/down dots and exchange mode badges.
- **Message records viewer** (Functional): Filterable, searchable message log with raw detail view.
- **Cancel All confirmation gate** (Learned Theme): Both Cancel All and Flatten All use `confirm()` dialogs with affected item counts -- satisfies the "destructive bulk actions require confirmation" theme.
- **Keyboard shortcut guard symmetry** (Learned Theme): Both Ctrl+B and Ctrl+S have identical `isBlockedField` guard conditions -- satisfies the "symmetric guards across paired actions" theme.
- **order_ack handling** (Learned Theme): `onBrokerMessage()` now processes `order_ack` messages -- satisfies the "protocol bridge layers should not silently discard message types" theme.
- **Notional value preview** (Informative): Order entry shows computed notional before submission.
- **P&L color coding and portfolio summary** (Informative): Green/red/grey PnL with equity curve chart and totals bar.
- **Bid/ask sizes and spread** (Informative): Market data shows size beneath price and spread column.
- **System clock and connection status** (Informative): Real-time clock and color-coded status dot in header.
- **Environment badge** (Informative): SIM/SANDBOX/PRODUCTION/FIX badge prominently displayed.
- **Order blotter rehydration** (Functional): Orders survive browser refresh via database replay.
- **Escape to close all modals** (Simple): Single keydown handler closes any open modal.
- **Overlay click to close** (Simple): All modals close when clicking the backdrop.
