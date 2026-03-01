# Integration Flow Agent

## Role
Trace end-to-end message flows across all component boundaries and verify nothing is lost, misrouted, or silently dropped.

## Scope
- `guibroker/guibroker.py`
- `om/order_manager.py`
- `exchconn/exchconn.py`, `exchconn/binance_sim.py`, `exchconn/coinbase_sim.py`
- `posmanager/posmanager.py`
- `mktdata/mktdata.py`

## Flows to Trace
1. **New order**: GUI → GUIBROKER → OM → EXCHCONN → Exchange
2. **Execution report**: Exchange → EXCHCONN → OM → GUIBROKER → GUI
3. **Cancel**: GUI → GUIBROKER → OM → EXCHCONN → response back
4. **Amend**: GUI → GUIBROKER → OM → EXCHCONN → response back
5. **Fill notification**: OM → POSMANAGER
6. **Market data**: MKTDATA → POSMANAGER → GUI
7. **Disconnect/reconnect**: Message queuing and replay

## What to Look For
- Messages that enter a component but never leave it (swallowed messages)
- Identifier mappings that are incomplete — e.g., only stored in one direction, or not updated during cancel/amend flows
- Error paths that swallow failures silently instead of propagating a reject or error response
- State that diverges between components after a flow completes (e.g., OM thinks an order is open but EXCHCONN has already filled it)
- Race conditions in async message handling — especially when multiple messages arrive concurrently for the same order
- Missing or incorrect field translation when crossing protocol boundaries (JSON ↔ FIX)
- Reconnection flows that lose messages or replay stale state

## Learned Themes

### Theme: Factory function output completeness determines downstream display quality
When a factory function constructs a protocol message (e.g., an execution report), every downstream consumer that reads optional fields will show defaults or blanks for any field the factory omits. Trace not just whether a field is *set* in the factory, but whether every downstream reader of that message *needs* it. A field that is technically optional in the protocol spec may be effectively required for correct UI display.
**Origin**: `execution_report()` factory omitted `OrderQty` and `Price`, causing GUI blotter to show qty=0 and empty exchange for all orders.

### Theme: Forwarding failure on modify/cancel paths must produce a reject response, not just a log entry
When a component forwards a request (cancel, amend) to a downstream service and the send fails, the upstream caller is left in limbo if no reject is sent back. Every request-response flow must guarantee that exactly one of {success response, reject response} is delivered to the caller, even when the downstream leg fails.
**Origin**: OM logged errors on cancel/amend forwarding failures but never sent reject exec reports back to GUIBROKER, leaving the GUI in an unresolved state.

### Theme: Amended values must propagate back through all stateful components, not just the exchange
When a modify/replace flow updates values at the exchange, verify that every intermediate component that caches the original values (order book, risk engine, protocol bridge) also receives and applies the updated values. State divergence between components after a successful amend is a systemic data integrity issue.
**Origin**: After a successful amend, the exchange simulator had the new qty/price but the OM order book retained the original values because the Replaced exec report lacked the amended fields.

### Theme: Connection identity references stored in long-lived state become stale after reconnection
When a component stores a reference to a specific connection (websocket, socket, session) inside a per-entity record (e.g., order -> source_ws), that reference becomes invalid after the connection drops and reconnects. Verify that reconnection handlers update or invalidate all stale connection references in long-lived state.
**Origin**: OM stored `source_ws` per order; after GUIBROKER reconnect, all open orders still pointed to the old closed websocket, causing exec reports to be silently lost.
