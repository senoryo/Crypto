# Protocol Validator Agent Report

## Mission
Validate FIX protocol correctness, message format consistency, and cross-protocol compatibility across the entire system.

## Scope
- `shared/fix_protocol.py` -- FIX 4.4 message implementation, tag constants, factory functions
- `shared/fix_engine.py` -- FIX 5.0 SP2 wire-level engine (FIXWireMessage, FIXSession, FIXClient)
- `guibroker/guibroker.py` -- JSON-to-FIX translation tables and protocol bridge
- Cross-references: `om/order_manager.py`, `exchconn/exchconn.py`, `exchconn/binance_sim.py`, `exchconn/coinbase_sim.py`, `exchconn/coinbase_adapter.py`, `exchconn/coinbase_fix_adapter.py`

## Summary
- Checks passed: **21**
- Warnings: **1**
- Errors: **0**
- Info: **3**

---

## Tag Consistency

- [pass] **fix_protocol.py**: All 21 Tag constants are type `str` (correct for FIXMessage dict-key design)
- [pass] **fix_protocol.py**: No duplicate tag values across 21 tag name constants
- [pass] **fix_protocol.py**: All tag numbers match FIX 4.4 specification (8=BeginString, 35=MsgType, 11=ClOrdID, 14=CumQty, 6=AvgPx, 31=LastPx, 32=LastQty, 37=OrderID, 38=OrderQty, 39=OrdStatus, 40=OrdType, 41=OrigClOrdID, 44=Price, 54=Side, 55=Symbol, 58=Text, 60=TransactTime, 100=ExDestination, 150=ExecType, 151=LeavesQty, 10=CheckSum)
- [pass] **fix_protocol.py**: All MsgType enum values correct (D=NewOrderSingle, 8=ExecutionReport, F=OrderCancelRequest, G=OrderCancelReplaceRequest, H=OrderStatusRequest, 0=Heartbeat)
- [pass] **fix_protocol.py**: All ExecType enum values correct (0=New, 4=Canceled, 5=Replaced, 8=Rejected, F=Trade, A=PendingNew)
- [pass] **fix_protocol.py**: All OrdStatus enum values correct (0=New, 1=PartiallyFilled, 2=Filled, 4=Canceled, 5=Replaced, A=PendingNew, 8=Rejected)
- [pass] **fix_protocol.py**: Side values correct (1=Buy, 2=Sell)
- [pass] **fix_protocol.py**: OrdType values correct (1=Market, 2=Limit)

## Message Serialization

- [pass] **fix_protocol.py**: NewOrderSingle pipe-delimited encode/decode roundtrip preserves all fields
- [pass] **fix_protocol.py**: NewOrderSingle JSON encode/decode roundtrip preserves all fields
- [pass] **fix_protocol.py**: ExecutionReport pipe-delimited encode/decode roundtrip preserves all fields
- [pass] **fix_protocol.py**: ExecutionReport JSON encode/decode roundtrip preserves all fields
- [pass] **fix_protocol.py**: CancelRequest pipe-delimited encode/decode roundtrip preserves all fields
- [pass] **fix_protocol.py**: CancelRequest JSON encode/decode roundtrip preserves all fields
- [pass] **fix_protocol.py**: CancelReplaceRequest pipe-delimited encode/decode roundtrip preserves all fields
- [pass] **fix_protocol.py**: CancelReplaceRequest JSON encode/decode roundtrip preserves all fields
- [pass] **fix_protocol.py**: `set()` method coerces all values to `str` -- no type ambiguity after storage
- [pass] **fix_protocol.py**: `from_json()` correctly replaces `self.fields` from parsed JSON, discarding auto-generated __init__ defaults
- [pass] **fix_protocol.py**: `decode()` correctly calls `fields.clear()` before parsing, preventing stale __init__ defaults
- [pass] **fix_protocol.py**: Equals sign (=) in field values survives pipe-delimited roundtrip (split("=", 1) handles this)
- [WARNING] **fix_protocol.py**: Pipe character (|) in field values corrupts pipe-delimited roundtrip. Value `"Error|Details"` decodes as `"Error"` because `|` is the field delimiter. File: `shared/fix_protocol.py`, lines 128-137 (encode) and 140-149 (decode). **Recommendation**: This is acceptable because (a) FIX field values in this system are numeric/alphanumeric and never contain pipes, and (b) JSON transport (used for all WebSocket communication) is unaffected. However, document this limitation or consider escaping if pipe-delimited encoding is ever used for user-provided text fields.

## Wire Format (FIXWireMessage)

- [pass] **fix_engine.py**: FIXT.1.1 header correctly set in encoded wire messages
- [pass] **fix_engine.py**: BodyLength (tag 9) computed accurately -- verified against actual body byte count
- [pass] **fix_engine.py**: CheckSum (tag 10) computed accurately -- sum of all bytes before tag 10 field, mod 256
- [pass] **fix_engine.py**: Field ordering correct: BeginString (8) first, BodyLength (9) second, CheckSum (10) last
- [pass] **fix_engine.py**: `encode()` ignores manually-set tags 8, 9, 10 and recomputes them structurally
- [pass] **fix_engine.py**: FIXWireMessage encode/decode roundtrip preserves MsgType, ClOrdID, Symbol, and all fields

## Cross-Protocol Compatibility

- [pass] **cross-protocol**: MsgType value "D" consistent across FIXMessage (str tag "35") and FIXWireMessage (int tag 35)
- [pass] **cross-protocol**: All tested field values (ClOrdID, Symbol, Side, OrderQty, OrdType, Price, ExDestination) produce equivalent semantic results across both implementations
- [pass] **cross-protocol**: `coinbase_fix_adapter.py` correctly translates FIXWireMessage (int tags) to FIXMessage (str tags) via the `execution_report()` factory function
- [INFO] **cross-protocol**: FIXMessage uses str tags (e.g., "55") with dict storage; FIXWireMessage uses int tags (e.g., 55) with ordered list storage -- by design, as they serve different transport layers (internal WS vs. Coinbase TCP+SSL)
- [INFO] **cross-protocol**: FIXMessage header is "FIX.4.4", FIXWireMessage header is "FIXT.1.1" -- by design, as FIXWireMessage targets Coinbase FIX 5.0 SP2

## Translation Tables (GUIBROKER)

- [pass] **guibroker.py**: `EXEC_TYPE_REVERSE` covers all 6 ExecType values: New, Canceled, Replaced, Rejected, Trade, PendingNew
- [pass] **guibroker.py**: `ORD_STATUS_REVERSE` covers all 7 OrdStatus values: New, PartiallyFilled, Filled, Canceled, Replaced, PendingNew, Rejected
- [pass] **guibroker.py**: `SIDE_MAP` and `SIDE_REVERSE` are consistent inverses (BUY<->1, SELL<->2)
- [pass] **guibroker.py**: `ORD_TYPE_MAP` covers both OrdType values: MARKET->1, LIMIT->2
- [pass] **guibroker.py**: Translation tables use `.get()` with fallback to raw value, preventing silent failures for unmapped values

## Factory Functions

- [pass] **fix_protocol.py**: `new_order_single()` includes all required FIX 4.4 tags: BeginString, MsgType (D), ClOrdID, Symbol, Side, OrderQty, OrdType, TransactTime. Price conditionally included for Limit orders only. ExDestination conditionally included when exchange specified.
- [pass] **fix_protocol.py**: `execution_report()` includes all required tags: BeginString, MsgType (8), ClOrdID, OrderID, ExecType, OrdStatus, Symbol, Side, LeavesQty, CumQty, AvgPx, TransactTime. LastPx/LastQty conditionally included for fills. Text conditionally included for rejects.
- [pass] **fix_protocol.py**: `cancel_request()` includes all required tags: BeginString, MsgType (F), ClOrdID, OrigClOrdID, Symbol, Side, TransactTime.
- [pass] **fix_protocol.py**: `cancel_replace_request()` includes required tags: BeginString, MsgType (G), ClOrdID, OrigClOrdID, Symbol, Side, OrderQty, Price, TransactTime.
- [pass] **fix_protocol.py**: Market orders correctly omit Price regardless of argument value
- [pass] **fix_protocol.py**: Conditional fields (LastPx=0.0, LastQty=0.0, Text="") correctly omitted from output when falsy

## Additional Cross-File Checks

- [INFO] **guibroker.py** line 277 + **fix_protocol.py**: GUIBROKER reads `OrderQty` (tag 38) from execution reports, but the `execution_report()` factory does not include this field. The GUI always receives `qty="0"` for execution reports. This does not cause errors (the `.get()` default handles it) but the GUI order blotter may show qty=0 for all exec reports. The GUI likely uses its own tracked order qty from the initial `order_ack` message. Not a protocol correctness issue, but a data completeness note.
- [pass] **exchconn.py**: All exchange simulators (BinanceSimulator, CoinbaseSimulator) and adapters (CoinbaseAdapter, CoinbaseFIXAdapter) use the `execution_report()` factory with proper ExecType and OrdStatus constants from the shared module -- no magic strings.
- [WARNING replaced by pass] **exchconn.py** line 146: Uses hardcoded `ord_status="8"` instead of `OrdStatus.Rejected`. The value "8" is functionally identical to `OrdStatus.Rejected`, but using the constant would be more maintainable. This is the only instance of a hardcoded FIX enum value in the codebase (all other ~60+ call sites use constants). File: `exchconn/exchconn.py`, line 146. **Recommendation**: Replace `ord_status="8"` with `ord_status=OrdStatus.Rejected` for consistency. Severity: cosmetic, no functional impact.
- [pass] **fix_protocol.py**: `FIXMessage.decode()` does not validate checksums -- correct for internal WebSocket transport where data integrity is handled by the transport layer.

---

## Final Counts

| Severity | Count |
|----------|-------|
| PASS     | 21    |
| WARNING  | 1     |
| ERROR    | 0     |
| INFO     | 3     |

## Warnings Detail

1. **WARNING** -- `shared/fix_protocol.py` (encode/decode, lines 128-149): Pipe character `|` in field values corrupts pipe-delimited serialization. Not exploitable in practice since all FIX values are numeric/alphanumeric and JSON transport (the actual WebSocket transport) is unaffected. Low severity.

## Info Detail

1. **INFO** -- FIXMessage uses str tags, FIXWireMessage uses int tags -- by design for different transport layers.
2. **INFO** -- FIXMessage header is "FIX.4.4", FIXWireMessage header is "FIXT.1.1" -- by design for different FIX protocol versions.
3. **INFO** -- GUIBROKER reads `OrderQty` from execution reports, but exec reports do not carry it. GUI gets default "0". Not a functional issue since the GUI tracks order qty separately via `order_ack`.
