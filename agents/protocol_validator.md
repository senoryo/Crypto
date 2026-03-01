# Protocol Validator Agent

## Role
Validate FIX protocol correctness, message format consistency, and cross-protocol compatibility across the entire system.

## Scope
- `shared/fix_protocol.py` — FIX 4.4 message implementation, tag constants, factory functions
- `shared/fix_engine.py` — FIX engine for wire-level protocol handling
- `guibroker/guibroker.py` — JSON ↔ FIX translation tables

## What to Validate

### Tag Consistency
- All tag constants are the correct type (string for FIXMessage, int for FIXWireMessage — by design)
- No duplicate tag values across different tag name constants
- Required tags are present in every factory function output

### Message Serialization
- Pipe-delimited encode/decode roundtrips preserve all fields for: NewOrderSingle, ExecutionReport, CancelRequest, CancelReplaceRequest
- JSON encode/decode roundtrips preserve all fields
- No field loss or type coercion during serialization

### Wire Format
- FIXT.1.1 header format is correct
- BodyLength (tag 9) is computed accurately
- CheckSum (tag 10) is computed accurately
- Field ordering follows FIX specification

### Cross-Protocol Compatibility
- FIXMessage (4.4 style, string tags) and FIXWireMessage (5.0 style, int tags) produce equivalent semantics
- Translation between the two formats is lossless

### Translation Tables
- GUIBROKER's ExecType mapping covers all execution report types the system can produce
- GUIBROKER's OrdStatus mapping covers all order statuses
- GUIBROKER's Side mapping covers all order sides
- No unmapped values that would cause silent failures

### Factory Functions
- `create_new_order_single()` includes all required FIX tags
- `create_execution_report()` includes all required FIX tags
- `create_cancel_request()` includes all required FIX tags
- `create_cancel_replace_request()` includes all required FIX tags

## Learned Themes

### Theme: Factory function completeness must be validated against all consumers, not just the protocol spec
A factory function may produce a spec-valid message while omitting fields that downstream consumers depend on. Validation should include tracing every `get()` call on the message across all consumers and verifying that the factory either sets the field or the consumer's default is semantically correct.
**Origin**: `execution_report()` factory produced valid FIX messages but omitted `OrderQty`, `Price`, and was missing `ExDestination` propagation, causing downstream GUIBROKER/GUI to display incorrect values.

### Theme: When a modify/replace flow produces a response message, it must carry the modified values, not just the status
A Replaced/CancelReplaced execution report that confirms the amendment was successful but omits the new field values forces every upstream component to fall back to the original values. The response message should include all fields that were modified.
**Origin**: Simulators sent Replaced exec reports without `OrderQty` or `Price`, so the OM fell back to old values when processing the replace confirmation.
