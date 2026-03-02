# Exchange Simulator Validation Report

**Date**: 2026-03-02
**Reviewer**: exchange-validator (exchange_adapter + bug_hunter perspectives)
**Scope**: 5 new exchange simulators + integration into exchconn.py and shared/config.py

## Files Reviewed

| File | Role |
|------|------|
| `exchconn/kraken_sim.py` | New Kraken simulator |
| `exchconn/bybit_sim.py` | New Bybit simulator |
| `exchconn/okx_sim.py` | New OKX simulator |
| `exchconn/bitfinex_sim.py` | New Bitfinex simulator |
| `exchconn/htx_sim.py` | New HTX simulator |
| `exchconn/exchconn.py` | Integration / routing |
| `shared/config.py` | Configuration / symbol mappings |
| `exchconn/binance_sim.py` | Reference implementation |
| `exchconn/coinbase_sim.py` | Reference implementation |

---

## Integration Checks -- All Pass

1. **Imports**: All 5 new simulators imported in `exchconn/exchconn.py` lines 24-28.
2. **Registration**: All 5 registered in `_exchanges` dict (lines 63-67): KRAKEN, BYBIT, OKX, BITFINEX, HTX.
3. **Report callbacks**: Set correctly for all exchanges via loop at lines 70-71.
4. **Start/Stop lifecycle**: All exchange `start()` and `stop()` called via loops at lines 205-206 and 218-219.
5. **Config entries**: All 5 exchanges present in `EXCHANGES` dict in `shared/config.py` lines 111-161 with proper symbol mappings.
6. **Default routing**: Updated to use new exchanges -- ETH->KRAKEN, ADA->OKX, DOGE->BYBIT (lines 164-170).
7. **Symbol mappings are realistic**: Kraken uses XXBTZUSD/XETHZUSD, Bitfinex uses tBTCUSD, HTX uses lowercase btcusdt, OKX uses BTC-USDT, Bybit uses BTCUSDT. All correct per real exchange APIs.

---

## Findings

### Finding 1 -- Unused `remaining` variable in `_execute_fill()`

- **Severity**: WARNING
- **Category**: Bug (Dead code)
- **Finding**: All 5 new simulators and both reference simulators assign `remaining = order.leaves_qty` at the top of `_execute_fill()` but never read the variable. The fill logic uses `order.leaves_qty` directly throughout.
- **Files**: All 7 simulator files, `_execute_fill()` method (e.g., `exchconn/kraken_sim.py:199`)
- **Proposed fix**: Remove the `remaining = order.leaves_qty` line.

### Finding 2 -- Unused imports: `time` and `MsgType`

- **Severity**: WARNING
- **Category**: Bug (Unused import)
- **Finding**: All 5 new simulators import `time` (line 14) and `MsgType` (from fix_protocol, line 18) but neither is used anywhere. The reference simulators have the same issue.
- **Files**: All 7 simulator files, lines 14 and 18
- **Proposed fix**: Remove `import time` and `MsgType` from the import list.

### Finding 3 -- Unused import: `EXCHANGES`

- **Severity**: WARNING
- **Category**: Bug (Unused import)
- **Finding**: All 5 new simulators import `EXCHANGES` from `shared.config` (line 21) but never reference it.
- **Files**: All 5 new simulator files, line 21
- **Proposed fix**: Remove the `EXCHANGES` import unless symbol mapping logic is planned.

### Finding 4 -- Amend price=0 edge case

- **Severity**: WARNING
- **Category**: Interface Compliance (Edge case)
- **Finding**: In `amend_order()`, when `new_qty` is 0 (default from `fix_msg.get(Tag.OrderQty, "0")`), the qty amendment is skipped. When `new_price` is 0, the price amendment is also skipped. This means you cannot amend a price to 0 (which would be valid for converting a limit to a market order on some exchanges). Consistent with both reference implementations and acceptable for simulators.
- **Files**: All 7 simulators, `amend_order()` method (e.g., `exchconn/kraken_sim.py:398-402`)
- **Proposed fix**: No change needed for simulation. If limit-to-market conversion is needed later, use a different sentinel value or separate flag.

### Finding 5 -- Zero-price fills for unknown symbols

- **Severity**: WARNING
- **Category**: Bug (Missing validation)
- **Finding**: `_get_current_price()` returns `0.0` for symbols not in `BASE_PRICES`. If a market order arrives for an unsupported symbol, fills execute at price `0.0`, producing economically nonsensical fills. The EXCHCONN layer does not validate symbol support before routing.
- **Files**: All 7 simulators, `_get_current_price()` (e.g., `exchconn/kraken_sim.py:81-82`); `exchconn/exchconn.py:126-140`
- **Proposed fix**: Add validation in `submit_order()` -- if `_get_current_price(symbol) <= 0`, reject the order with text "Unsupported symbol". Alternatively, add symbol validation in EXCHCONN before routing.

---

## Positive Findings (INFO)

### Interface Parity -- PASS
All 5 new simulators correctly implement the full interface: `submit_order`, `cancel_order`, `amend_order`, `set_report_callback`, `start`, `stop`. Method signatures are identical to the Binance/Coinbase reference implementations.

### FIX Execution Report Construction -- PASS
All reports include required tags (ClOrdID, OrderID, ExecType, OrdStatus, Symbol, Side, LeavesQty, CumQty, AvgPx). Trade reports include LastPx and LastQty. Amend reports include both OrderQty and Price as separate `.set()` calls. ExDestination is set on all outbound reports via `_send_report()`.

### Order ID Prefixes -- PASS
Correct and distinct prefixes: KRK-, BYB-, OKX-, BFX-, HTX-. No collision with existing BIN- and CB- prefixes.

### Partial Fill Logic -- PASS
Orders fill in 1-3 chunks with realistic randomization. `leaves_qty` decremented after each chunk. `cum_qty` and `avg_px` accumulated correctly using VWAP formula. Epsilon comparison (`<= 1e-10`) correctly handles float precision for final fill detection. `is_active` set to False on final fill.

### Order State Transitions -- PASS
Correct transitions observed: New -> PartiallyFilled -> Filled, New -> Canceled, New -> Replaced. Rejects sent for cancel/amend of inactive orders with appropriate text.

### Lifecycle Cleanup -- PASS
`stop()` cancels the price task, cancels all fill tasks, awaits them (catching CancelledError), and clears the fill_tasks dict. No resource leaks. `_fill_tasks.pop()` in the `finally` block of `_execute_fill()` ensures cleanup even on exceptions.

### Race Condition Handling -- PASS
Cancel and amend both: (1) cancel the fill task, (2) await its completion, (3) then modify order state. This prevents the fill task from racing against the cancel/amend operation. The `is_active` flag check at each await point in `_execute_fill()` provides a secondary guard.

### Exchange Differentiation -- PASS
Each simulator has unique behavioral parameters:
| Exchange | Jitter | Fill Delay | Price Variation |
|----------|--------|------------|-----------------|
| Kraken | 0.08% | 0.3-1.5s | +-0.015% |
| Bybit | 0.10% | 0.3-1.5s | +-0.02% |
| OKX | 0.09% | 0.5-2.0s | +-0.02% |
| Bitfinex | 0.07% | 0.5-2.5s | +-0.025% |
| HTX | 0.15% | 0.8-3.0s | +-0.03% |

---

## Summary

| Severity | Count | Key Items |
|----------|-------|-----------|
| CRITICAL | 0 | None |
| WARNING | 5 | Unused variable (#1), unused imports (#2, #3), amend edge case (#4), zero-price fills (#5) |
| INFO | 8 | All positive -- interface parity, FIX compliance, prefixes, fills, state transitions, cleanup, race handling, differentiation |

**Overall**: The 5 new simulators are well-implemented, closely following the established reference pattern. Code is structurally sound with correct async handling, proper task cleanup, and correct FIX protocol compliance. The most actionable finding is #5 (zero-price fills for unknown symbols). Findings #1-#3 are cosmetic cleanup. No critical bugs or async pitfalls found.
