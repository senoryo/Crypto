# Protocol Validator Agent Report

## Date: 2026-03-01

## Summary

Full re-validation of FIX protocol correctness across `shared/fix_protocol.py`, `shared/fix_engine.py`, and `guibroker/guibroker.py`, with cross-reference tracing into all consumers (OM, EXCHCONN, simulators, real adapters, GUI). Tag constants, serialization roundtrips, wire format, and translation tables are all clean. Two previously reported MEDIUM issues (CoinbaseAdapter and CoinbaseFIXAdapter missing OrderQty/Price on Replaced exec reports) have been fixed since the last report. One remaining MEDIUM issue: the `execution_report()` factory omits OrderQty/Price for non-Replaced reports, causing the GUI order blotter to overwrite correct qty/price with 0 when the New acknowledgment exec report arrives.

## Findings

### [MEDIUM] New/Trade exec reports omit OrderQty and Price, causing GUI blotter qty=0

- **Category**: Factory Function Completeness / Learned Theme #1
- **Location**: `shared/fix_protocol.py:205-228` (factory), `guibroker/guibroker.py:301` (consumer), `gui/app.js:395` (display)
- **Issue**: The `execution_report()` factory conditionally sets `Tag.OrderQty` and `Tag.Price` only when the values are non-zero (lines 225-228). For New acknowledgment and Trade exec reports, callers typically pass `order_qty=0.0` (the default) because they are not amend responses. This means the GUIBROKER reads `fix_msg.get(Tag.OrderQty, "0")` at line 301 and sends `qty: 0` to the GUI for every New exec report.
- **Data flow**:
  1. GUI receives `order_ack` from GUIBROKER with correct `qty` (guibroker.py:183) -- order appears in blotter with correct qty.
  2. Exchange simulator sends New exec report via `execution_report()` without `order_qty` parameter (e.g., binance_sim.py:181).
  3. OM forwards exec report to GUIBROKER.
  4. GUIBROKER translates to JSON with `qty: 0` (guibroker.py:301).
  5. GUI receives exec report where `data.qty` is `0` (not null). At app.js:395, `data.qty != null ? data.qty : (existing.qty || 0)` evaluates to `0`, overwriting the correct qty from the order_ack.
- **Impact**: GUI order blotter shows `qty=0` after the New acknowledgment arrives, breaking fill percentage display (app.js:451-455 divides by `o.qty`) and showing incorrect values in the qty column.
- **Suggested fix (choose one)**:
  - **Option A (factory)**: All callers of `execution_report()` for New/Trade should pass `order_qty=` and `price=` from the tracked order state. This requires updating all call sites in both simulators, the CoinbaseAdapter, and the CoinbaseFIXAdapter.
  - **Option B (GUIBROKER)**: In the GUIBROKER's `_handle_execution_report()`, check if OrderQty is absent/zero and omit the `qty` key from the JSON entirely (or use `None`). Then the GUI's null check at app.js:395 would preserve the existing value.
  - **Option C (GUI)**: Change app.js:395 to `data.qty > 0 ? data.qty : (existing.qty || 0)`, treating 0 as "not provided" and preserving the existing value.
- **Note**: The OM is unaffected because it independently tracks `order["qty"]` from the original NewOrderSingle and uses fallback logic at line 635.

### [LOW] FIXMessage.decode() does not validate checksum

- **Category**: Message Serialization
- **Location**: `shared/fix_protocol.py:140-149`
- **Issue**: `FIXMessage.decode()` parses the checksum tag from the raw string and stores it as a regular field, but never validates it against a recomputed checksum. A corrupted message would decode silently with incorrect data.
- **Impact**: Low in practice because FIXMessage pipe-delimited encoding is only used internally (all WebSocket transport uses JSON via `to_json()`/`from_json()`, which provides its own framing). If pipe-delimited encoding were ever used for transport, corrupted messages would be accepted without error.
- **Suggested Fix**: No immediate action required. Document this limitation in a code comment if desired.

### [LOW] Pipe character in field values corrupts pipe-delimited serialization

- **Category**: Message Serialization
- **Location**: `shared/fix_protocol.py:128-149`
- **Issue**: The pipe character `|` is used as the field delimiter in `encode()`. If any field value contains a pipe (e.g., `Text` field with "Error|Details"), the value is split during `decode()`, producing corrupted fields.
- **Impact**: Low in practice because (1) all FIX field values in this system are numeric or alphanumeric identifiers, (2) all WebSocket transport uses JSON (`to_json()`/`from_json()`) which is unaffected, and (3) FIX spec Text fields in this system contain short error messages without pipe characters.
- **Suggested Fix**: No immediate action required.

## Previously Reported Issues — Now Fixed

### [FIXED] CoinbaseAdapter Replaced exec report missing OrderQty/Price

- **Previous severity**: MEDIUM
- **Location**: `exchconn/coinbase_adapter.py:377-378`
- **Status**: Fixed. Lines 377-378 now add `report.set(Tag.OrderQty, ...)` and `report.set(Tag.Price, ...)` after constructing the Replaced exec report, matching the pattern used by both simulators.

### [FIXED] CoinbaseFIXAdapter Replaced exec report missing OrderQty/Price

- **Previous severity**: MEDIUM
- **Location**: `exchconn/coinbase_fix_adapter.py:327-348`
- **Status**: Fixed. Lines 327-328 now read `wire_order_qty = wire.get_float(38)` and `wire_price = wire.get_float(44)` from the incoming wire message, and lines 345-348 set them on the internal report when non-zero.

## No Issues Found

The following validation categories were clean:

- **Tag Consistency**: All 21 Tag constants are correct type (`str` for FIXMessage dict keys). No duplicate tag values across all 21 constants (verified programmatically). All tag numbers match FIX 4.4 specification. All enum classes (MsgType, ExecType, OrdStatus, Side, OrdType) have correct values matching FIX 4.4.

- **Wire Format (FIXWireMessage)**: FIXT.1.1 header is correct. BodyLength (tag 9) is computed accurately against actual body byte count (verified by re-measurement). CheckSum (tag 10) is computed as sum of all preceding bytes mod 256 (verified by recomputation). Field ordering follows FIX spec (8 first, 9 second, 10 last). `encode()` correctly ignores manually-set tags 8/9/10 and recomputes them.

- **JSON Serialization Roundtrip**: `to_json()`/`from_json()` roundtrips preserve all fields for all message types (NewOrderSingle, ExecutionReport, CancelRequest, CancelReplaceRequest). `from_json()` correctly replaces `self.fields` from parsed JSON. No type coercion issues since `set()` coerces all values to `str`.

- **Pipe-Delimited Serialization Roundtrip**: `encode()`/`decode()` roundtrips preserve all fields for all message types when values contain no pipe characters (verified programmatically for all 4 message types). `decode()` correctly calls `fields.clear()`. Equals sign in values survives via `split("=", 1)`.

- **Cross-Protocol Compatibility**: FIXMessage (str tags, dict storage, FIX.4.4) and FIXWireMessage (int tags, ordered list storage, FIXT.1.1) produce equivalent semantics. `coinbase_fix_adapter.py` correctly translates between the two via field-by-field extraction and `execution_report()` factory construction. The adapter now correctly propagates OrderQty (tag 38) and Price (tag 44) from wire messages. MsgType values are consistent across both implementations.

- **Translation Tables (GUIBROKER)**: `EXEC_TYPE_REVERSE` covers all 6 ExecType values (New, Canceled, Replaced, Rejected, Trade, PendingNew). `ORD_STATUS_REVERSE` covers all 7 OrdStatus values (New, PartiallyFilled, Filled, Canceled, Replaced, PendingNew, Rejected). `SIDE_MAP`/`SIDE_REVERSE` are consistent inverses. `ORD_TYPE_MAP` covers both order types. All maps use `.get()` with fallback to raw value, preventing silent failures for unmapped values. Verified programmatically that every defined enum value has a mapping.

- **Factory Functions (Required Tags)**: `new_order_single()` includes all required FIX 4.4 tags for MsgType=D (BeginString, MsgType, ClOrdID, Symbol, Side, OrderQty, OrdType, TransactTime; Price conditional for Limit). `cancel_request()` includes all required tags for MsgType=F (BeginString, MsgType, ClOrdID, OrigClOrdID, Symbol, Side, TransactTime). `cancel_replace_request()` includes all required tags for MsgType=G (BeginString, MsgType, ClOrdID, OrigClOrdID, Symbol, Side, OrderQty, Price, TransactTime). Market orders correctly omit Price via conditional `if ord_type == OrdType.Limit`. All exchange code uses shared constants, no magic strings for FIX enum values.

- **FIXSession (fix_engine.py)**: Sequence numbering starts at 1 (first call to `next_send_seq()` returns 1, correct per FIX spec). `stamp_message()` correctly adds SenderCompID (49), TargetCompID (56), MsgSeqNum (34), and SendingTime (52). `reset()` properly zeros both send and receive counters. Send lock in `FIXClient.send()` ensures atomic stamp+write (no sequence number gaps under concurrent sends).

## Learned Theme Validation

### Theme 1: Factory function completeness must be validated against all consumers

Re-validated. The `execution_report()` factory now supports `order_qty` and `price` parameters (added since the previous cycle). However, callers only pass these for Replaced reports. For New and Trade exec reports, `order_qty=0.0` is the default, so the tag is never set. The GUIBROKER unconditionally reads `Tag.OrderQty` for ALL exec report types (not just Replaced), sending `qty: 0` to the GUI. The structural fix should either be in caller discipline (always pass order_qty for all exec types) or in GUIBROKER/GUI logic to handle absent values gracefully.

### Theme 2: Modify/replace responses must carry modified values

Re-validated. All four exchange adapters now correctly include `OrderQty` and `Price` in Replaced exec reports:
- BinanceSimulator: lines 435-436
- CoinbaseSimulator: lines 424-425
- CoinbaseAdapter: lines 377-378 (fixed since last report)
- CoinbaseFIXAdapter: lines 345-348 (fixed since last report)

This theme is fully resolved.

## Final Counts

| Severity | Count |
|----------|-------|
| MEDIUM   | 1     |
| LOW      | 2     |
| FIXED    | 2 (from previous report) |
| CLEAN    | 8 categories |
