# UX Reviewer Agent Report

## Mission

Review the trading UI (`gui/index.html`, `gui/app.js`, `gui/styles.css`) from an experienced trader's perspective, evaluating against four pillars: **INTUITIVE**, **SIMPLE**, **INFORMATIVE**, **FUNCTIONAL**.

## Files Reviewed

- `/mnt/c/Users/yoavh/Projects/Crypto/gui/index.html` (322 lines)
- `/mnt/c/Users/yoavh/Projects/Crypto/gui/app.js` (1677 lines)
- `/mnt/c/Users/yoavh/Projects/Crypto/gui/styles.css` (1579 lines)

---

## Pillar Scores

| Pillar | Score | Pass | Warn | Info |
|--------|-------|------|------|------|
| INTUITIVE | 95% | 13 | 1 | 0 |
| SIMPLE | 86% | 6 | 1 | 1 |
| INFORMATIVE | 88% | 14 | 2 | 1 |
| FUNCTIONAL | 93% | 14 | 1 | 1 |
| **OVERALL** | **90%** | **47** | **5** | **3** |

---

## Strengths (47 items)

### INTUITIVE (13 strengths)

1. **Click-to-trade workflow**: Clicking a market data row calls `selectSymbol(sym)` (app.js:213-216) which sets `dom.oeSymbol.value = sym` and auto-populates the price field from live bid/ask depending on side. This is the core click-to-trade pattern professional traders expect.

2. **Price auto-populates on click**: `selectSymbol()` (app.js:234-242) fills the limit price with the bid (for BUY) or ask (for SELL) from live market data. Reduces manual entry and prevents stale-price errors.

3. **BUY/SELL visually distinct**: BUY button uses `--green` (#3fb950) and SELL uses `--red` (#f85149) (styles.css:424-432). Universal trading color convention — no ambiguity on trade direction.

4. **Submit button reflects side**: `setSide()` (app.js:296-309) changes both the label (`"BUY"` / `"SELL"`) and the CSS class (`submit-btn buy` / `submit-btn sell`) on the submit button. Prevents accidental wrong-side orders.

5. **Price field disabled for market orders**: `onOrdTypeChange()` (app.js:311-316) disables and clears the price input when MARKET is selected. Eliminates confusion about whether a price is required.

6. **Double-click to amend**: The blotter row `ondblclick` attribute (app.js:447) opens the amend modal for active orders. Natural interaction pattern that experienced traders expect.

7. **Position direction labeled (Long/Short/Flat)**: `renderPositions()` (app.js:651-662) labels positions as `LONG`, `SHORT`, or `FLAT` with corresponding color classes `dir-long` (green), `dir-short` (red), `dir-flat` (muted). Instant visual reading of portfolio direction.

8. **P&L color-coded green/red**: `pnlClass()` (app.js:854-858) returns `pnl-positive` (green), `pnl-negative` (red), or `pnl-zero` (muted). Applied to both individual position P&L and portfolio totals.

9. **Price flash on tick**: Market data rows flash green or red for 400ms on price change (app.js:201-207, styles.css:333-349). Shows direction of last trade instantly.

10. **Status color-coded in blotter**: Six order status classes: `status-new` (blue), `status-partial` (yellow), `status-filled` (green), `status-canceled`/`status-rejected` (red), `status-pending` (orange) (styles.css:508-528). Traders can scan blotter status at a glance.

11. **Newest orders at top**: `renderBlotter()` calls `rows.reverse()` (app.js:421) to display newest orders first. Matches trader expectation that recent activity is top-of-screen.

12. **Selected row highlighting**: Clicked market data row gets `.selected` class with `--blue-bg` background (styles.css:280-282). Clear visual feedback for which symbol is active.

13. **Amend modal pre-fills current values**: `openAmendModal()` (app.js:571-584) populates quantity and price from the existing order. Reduces error risk during amendments.

### SIMPLE (6 strengths)

14. **Dark theme**: Background `--bg-primary: #0d1117`, text `--text-primary: #e6edf3` (styles.css:9-10). Industry standard for trading terminals; reduces eye strain during long sessions.

15. **Monospace fonts for numerical data**: All tables and inputs use `var(--font-mono): 'Consolas', 'Monaco', 'Courier New', monospace` (styles.css:28). Digits align vertically for easy comparison across rows.

16. **Fixed viewport layout**: `html, body { overflow: hidden; }` and `height: 100vh` grid (styles.css:40-50). No unnecessary scrolling — everything fits on one screen like Bloomberg or Fidessa.

17. **Keyboard shortcuts**: Ctrl+B for BUY, Ctrl+S for SELL, Escape to close modals (app.js:1607-1630). Essential for fast execution without mouse.

18. **Form clears after submission**: `submitOrder()` resets quantity field after successful submission (app.js:357). Ready for next order immediately.

19. **Single-page layout**: All four panels (Market Data, Order Entry, Blotter, Positions) visible simultaneously with no tab switching for primary workflow. This is the gold standard for trading terminals.

### INFORMATIVE (14 strengths)

20. **Market data columns**: Table shows Symbol, Bid, Ask, Spread, Last, Mid, Volume (index.html:37-45). Covers essential pricing data.

21. **Bid/ask sizes displayed**: Bid and ask sizes are rendered inline beneath the price in a compact format (app.js:174-184). Shows available liquidity at each level.

22. **Spread column**: Spread is computed as `ask - bid` and shown in the market data table in yellow (app.js:171, styles.css:311-314). Critical for assessing execution cost.

23. **Mid-price displayed**: Mid price `(bid + ask) / 2` is calculated and shown (app.js:172). Reference price for fair value.

24. **Notional value preview**: `updateNotionalDisplay()` (app.js:263-282) shows `"Notional: $X.XX"` before submission. Helps traders gauge USD exposure.

25. **Spread in order entry**: Current spread shown next to price label (app.js:252-261). Contextual cost information at point of entry.

26. **Order blotter columns**: 11 columns: ClOrdID, Symbol, Side, Qty, Price, Type, Status, Filled, AvgPx, Exchange, Actions (index.html:129-141). Full lifecycle visibility.

27. **Fill progress display**: Shows `filled/total (pct%)` format (app.js:438-445). Traders can see partial fill progress.

28. **Order count in header**: `blotterCount` updates with count of orders (app.js:423). Quick gauge of activity level.

29. **Total portfolio P&L summary**: Summary bar shows Equity, Unrealized, and Realized P&L (index.html:188-192, app.js:677-684). Portfolio-level view at a glance.

30. **System clock**: Real-time clock in header updated every second (app.js:69-78). Traders reference time for market events.

31. **Connection status indicator**: Status button dot changes green/yellow/red based on WebSocket connectivity (styles.css:158-183, app.js:1178-1197). Immediate awareness of data freshness.

32. **Environment badge**: Shows SIM/SANDBOX/PRODUCTION/FIX mode prominently in header (app.js:1233-1261). Critical for knowing if trading real money.

33. **Equity curve chart**: Canvas-based P&L chart (app.js:742-803) plots equity over time with zero-line reference. Visual trend of session performance.

### FUNCTIONAL (14 strengths)

34. **WebSocket auto-reconnection**: `connectWS()` (app.js:84-116) reconnects after 3-second delay on disconnect. Trading resumes automatically after network drops.

35. **Toast notifications**: `showToast()` (app.js:879-893) displays non-blocking notifications for fills, rejects, and errors with auto-dismiss after 3 seconds. Three types: success (green), error (red), info (blue).

36. **Cancel button on active orders**: Cancel buttons appear only on orders with status NEW, PARTIALLY_FILLED, or PENDING_NEW (app.js:428-436).

37. **Amend button on active orders**: Amend buttons appear alongside cancel for active orders (app.js:434).

38. **Action buttons disabled on terminal states**: Actions rendered only for active orders; filled/canceled/rejected rows have no action buttons (app.js:428-436).

39. **Amend modal with validation**: `submitAmend()` validates quantity > 0 before sending (app.js:601-607). Prevents invalid amendments.

40. **Risk limits editable at runtime**: Risk modal reads and saves limits via `/api/risk-limits` REST endpoint (app.js:1281-1386). No restart required.

41. **System status modal**: Architecture diagram shows all 6 components with up/down status dots and exchange mode badges (app.js:1004-1172).

42. **Message records viewer**: Full-featured records modal with component filter, direction filter, text search, row limit, and raw message detail view (app.js:1484-1601).

43. **Cancel All**: `cancelAllOrders()` (app.js:513-540) iterates active orders and sends cancel for each. Emergency panic button.

44. **Flatten All**: `flattenAll()` (app.js:702-736) sends market orders to close all open positions. Emergency exit.

45. **Order blotter rehydration**: `rehydrateFromDB()` (app.js:1636-1657) replays execution reports from message database on page load. Orders survive browser refresh.

46. **Modal close on overlay click**: All modals close when clicking the overlay background (app.js:624-626, 915-917, 1042-1044, 1299-1301, 1406-1408, 1499-1501).

47. **Escape key closes all modals**: Single keydown handler (app.js:1609-1616) closes any open modal on Escape. Universal dismiss pattern.

---

## Issues and Suggestions

### WARNING (5 items)

#### W-1: No confirmation on destructive bulk actions
- **Pillar**: FUNCTIONAL
- **File**: `gui/app.js` lines 513-540, 702-736
- **Description**: `cancelAllOrders()` and `flattenAll()` execute immediately with no confirmation dialog. A single accidental click can cancel every open order or flatten the entire portfolio. While speed is important, these are emergency-level actions with potentially significant financial impact.
- **Recommendation**: Add a brief confirmation prompt (e.g., `"Cancel 5 active orders? [Yes/No]"`) only for bulk actions. Single cancel/amend should remain instant. Consider requiring a double-click or a brief hold-to-confirm interaction to prevent accidental triggers while keeping latency low.

#### W-2: No high/low or 24h range in market data
- **Pillar**: INFORMATIVE
- **File**: `gui/index.html` lines 36-46, `gui/app.js` lines 122-218
- **Description**: The market data table shows Bid, Ask, Spread, Last, Mid, Volume but no high/low prices or 24h range. Traders use high/low to gauge whether current price is near the day's extreme and to set limit prices intelligently.
- **Recommendation**: Add High and Low columns to the market data table. If the data feed provides them (check `mktdata` component), display them. If not, track the high/low from the `last` prices observed during the session. Even session-high/low is more useful than nothing.

#### W-3: No keyboard shortcut for order submission
- **Pillar**: SIMPLE
- **File**: `gui/app.js` lines 1607-1630
- **Description**: Keyboard shortcuts exist for BUY side (Ctrl+B) and SELL side (Ctrl+S), but there is no shortcut to submit the order (e.g., Ctrl+Enter or Enter when the form is focused). A trader who uses keyboard shortcuts for side selection still has to reach for the mouse to click the submit button or rely on form-native Enter behavior (which only works when focus is on a form field).
- **Recommendation**: Add Ctrl+Enter as a global keyboard shortcut to submit the current order form. This completes the keyboard-only workflow: Ctrl+B to set side, type quantity, Ctrl+Enter to fire.

#### W-4: Ctrl+S (SELL shortcut) only works outside input fields
- **Pillar**: SIMPLE / INTUITIVE
- **File**: `gui/app.js` lines 1617-1629
- **Description**: The Ctrl+S shortcut for SELL is guarded by a check that the active element is not an INPUT or TEXTAREA (app.js:1624). This is to avoid intercepting the browser's Save dialog when editing text. However, Ctrl+B (BUY) has no such guard. This asymmetry means: (a) Ctrl+B triggers BUY even when the trader is typing in the quantity field, which could be surprising, and (b) Ctrl+S does NOT switch to SELL when the trader has focus in the quantity field, which is exactly when they most need it.
- **Recommendation**: Apply the same guard to Ctrl+B, or remove the guard from Ctrl+S. The preferred approach: guard both with a check that excludes only the troubleshoot textarea and the records search, not the order form inputs (where switching side mid-entry is intentional).

#### W-5: No position weights or allocation percentages
- **Pillar**: INFORMATIVE
- **File**: `gui/index.html` lines 170-183, `gui/app.js` lines 640-696
- **Description**: The positions table shows per-symbol Qty, Avg Cost, Mkt Price, Unrealized P&L, Realized P&L. However, there is no column showing position weight as a percentage of total portfolio value. Traders managing multiple positions need to know portfolio concentration at a glance.
- **Recommendation**: Add a "Weight %" column to the positions table, calculated as `abs(qty * mkt_price) / sum(abs(qty * mkt_price)) * 100`. This is a simple computation that provides significant portfolio management value.

### INFO (3 items)

#### I-1: Order entry form has 6 fields (acceptable but borderline)
- **Pillar**: SIMPLE
- **File**: `gui/index.html` lines 58-108
- **Description**: The order form has: Symbol, Type, Exchange, Side, Quantity, Price. Six fields is the minimum for a multi-exchange, multi-asset trading terminal. The Exchange selector (AUTO/BINANCE/COINBASE) adds a field that most retail terminals omit, but is justified here given the multi-exchange architecture.
- **Note**: The current field count is acceptable. AUTO as default exchange mitigates the extra field. No action needed unless the form grows further.

#### I-2: No sorting/filtering controls in the order blotter table headers
- **Pillar**: FUNCTIONAL
- **File**: `gui/index.html` lines 127-144, `gui/app.js` lines 414-465
- **Description**: The order blotter renders orders newest-first but there are no clickable column headers for sorting (e.g., sort by symbol, status, or filled percentage) and no filter controls (e.g., filter to show only active orders). The Records modal has filtering, but the main blotter does not. For a small order count this is fine; it becomes a pain point at scale.
- **Note**: Low priority. The blotter already shows orders in a useful default order. Filtering by active-only would be the highest-value addition if the blotter grows beyond ~50 rows.

#### I-3: Toast notifications lack a counter or stacking limit
- **Pillar**: FUNCTIONAL
- **File**: `gui/app.js` lines 879-893
- **Description**: Each toast is added to `#toast-container` and auto-removed after 3.3 seconds. If many fills arrive in rapid succession (e.g., 20 partial fills), 20 toasts stack up simultaneously. There is no stacking limit, no aggregation (e.g., "5 fills"), and no way to dismiss them early (no click-to-close).
- **Note**: Acceptable for normal operation. Could become visually noisy during high-activity bursts.

---

## Final Summary

The trading UI is **well-built and demonstrates strong alignment with professional trading terminal conventions**. The four-panel layout (Market Data + Order Entry on the left, Blotter + Positions on the right) follows the established pattern used by Bloomberg, Fidessa, and modern crypto exchanges. The dark theme, monospace numerical formatting, color-coded BUY/SELL, and flash-on-tick behavior are all standard and implemented correctly.

**Key strengths**: Click-to-trade workflow with auto-price population, comprehensive order lifecycle display with fill progress, portfolio P&L summary with equity curve, robust WebSocket reconnection, and full message audit trail in the Records modal. The risk limits editor and system architecture diagram are particularly impressive features for an independent trading platform.

**Areas for improvement**: The most actionable improvements are (1) adding a confirmation gate on the Cancel All and Flatten All bulk actions to prevent accidental portfolio-wide impacts, (2) adding high/low prices to market data, and (3) completing the keyboard-only order entry workflow with a submit shortcut. The Ctrl+B/Ctrl+S shortcut asymmetry (W-4) is a minor but correctness-relevant bug.

**Overall assessment**: 90% -- production-quality trading UI with minor gaps in data completeness and a few workflow polish items. No critical issues found.
