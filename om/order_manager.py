"""
Order Manager (OM) - Central order routing and management engine.

Responsibilities:
- Accept FIX messages from GUIBROKER (WS server on port 8083)
- Maintain internal order book
- Route orders to EXCHCONN (WS client to port 8084)
- Forward execution reports back to GUIBROKER
- Send fill notifications to POSMANAGER (WS client to port 8085)
- Handle cancel and amend requests
- Perform basic risk checks before routing

Run with: python -m om.order_manager
"""

import asyncio
import json
import logging
import signal
import sys
import time

from shared.config import (
    HOST, PORTS, SYMBOLS, EXCHANGES, DEFAULT_ROUTING,
    ORD_TYPE_LIMIT, ORD_TYPE_MARKET, SIDE_BUY,
)
from shared import risk_limits
from shared.fix_protocol import (
    FIXMessage, Tag, MsgType, ExecType, OrdStatus, OrdType,
    execution_report,
)
from shared.logging_config import setup_component_logging, log_recv, log_send
from shared.ws_transport import WSServer, WSClient, json_msg

logger = setup_component_logging("OM")


class OrderManager:
    """Central order routing and management engine."""

    def __init__(self):
        # WS server for GUIBROKER connections (port 8083)
        self.server = WSServer(HOST, PORTS["OM"], name="OM-Server")

        # WS client to EXCHCONN (port 8084)
        self.exchconn_client = WSClient(
            f"ws://{HOST}:{PORTS['EXCHCONN']}", name="OM->EXCHCONN"
        )

        # WS client to POSMANAGER (port 8085)
        self.pos_client = WSClient(
            f"ws://{HOST}:{PORTS['POSMANAGER']}", name="OM->POSMANAGER"
        )

        # Internal order book: cl_ord_id -> order dict
        self.orders: dict[str, dict] = {}
        self._orders_lock = asyncio.Lock()

        # OM order ID counter
        self._order_id_counter = 0

        # Map OM order IDs back to cl_ord_ids for reverse lookup
        self._om_id_to_cl_ord_id: dict[str, str] = {}

        # Net position per symbol tracked from fills (+long, -short)
        self._positions: dict[str, float] = {}
        self._positions_lock = asyncio.Lock()

        # Register handlers
        self.server.on_message(self._handle_guibroker_message)
        self.server.on_connect(self._handle_guibroker_connect)
        self.server.on_disconnect(self._handle_guibroker_disconnect)
        self.exchconn_client.on_message(self._handle_exchconn_message)

    def _next_order_id(self) -> str:
        """Generate the next OM order ID."""
        self._order_id_counter += 1
        return f"OM-{self._order_id_counter:06d}"

    # ---------------------------------------------------------------
    # Risk checks
    # ---------------------------------------------------------------

    def _validate_order(self, fix_msg: FIXMessage) -> str | None:
        """Validate an incoming order. Returns reject reason or None if valid."""
        symbol = fix_msg.get(Tag.Symbol)
        if symbol not in SYMBOLS:
            return f"Unknown symbol: {symbol}"

        try:
            qty = float(fix_msg.get(Tag.OrderQty, "0"))
        except ValueError:
            return f"Invalid quantity: {fix_msg.get(Tag.OrderQty)}"
        if qty <= 0:
            return f"Quantity must be positive, got {qty}"

        ord_type = fix_msg.get(Tag.OrdType)
        price = 0.0
        if ord_type == OrdType.Limit:
            try:
                price = float(fix_msg.get(Tag.Price, "0"))
            except ValueError:
                return f"Invalid price: {fix_msg.get(Tag.Price)}"
            if price <= 0:
                return f"Limit order price must be positive, got {price}"
        else:
            try:
                price = float(fix_msg.get(Tag.Price, "0"))
            except ValueError:
                price = 0.0

        exchange = self._resolve_exchange(fix_msg)
        if exchange not in EXCHANGES:
            return f"Unknown exchange: {exchange}"

        # Pre-trade risk checks
        limits = risk_limits.load_limits()
        side = fix_msg.get(Tag.Side)
        open_count = sum(
            1 for o in self.orders.values()
            if o["status"] in (OrdStatus.New, OrdStatus.PendingNew, OrdStatus.PartiallyFilled)
        )
        risk_reject = risk_limits.check_order(
            limits, symbol, side, qty, price, ord_type, self._positions, open_count,
        )
        if risk_reject:
            return risk_reject

        return None

    def _resolve_exchange(self, fix_msg: FIXMessage) -> str:
        """Resolve the target exchange for an order."""
        ex_dest = fix_msg.get(Tag.ExDestination)
        if ex_dest:
            return ex_dest
        symbol = fix_msg.get(Tag.Symbol)
        return DEFAULT_ROUTING.get(symbol, "")

    # ---------------------------------------------------------------
    # GUIBROKER message handling (server side, port 8083)
    # ---------------------------------------------------------------

    async def _handle_guibroker_connect(self, websocket):
        logger.info(f"GUIBROKER client connected: {websocket.remote_address}")
        # Update source_ws for all open orders to the new connection,
        # so exec reports for pre-existing orders are delivered correctly
        updated = 0
        async with self._orders_lock:
            for order in self.orders.values():
                if order["status"] in (OrdStatus.New, OrdStatus.PendingNew, OrdStatus.PartiallyFilled):
                    order["source_ws"] = websocket
                    updated += 1
        if updated:
            logger.info(f"Updated source_ws for {updated} open orders to new GUIBROKER connection")

    async def _handle_guibroker_disconnect(self, websocket):
        logger.info(f"GUIBROKER client disconnected: {websocket.remote_address}")

    async def _handle_guibroker_message(self, websocket, message: str):
        """Process incoming FIX messages from GUIBROKER."""
        try:
            fix_msg = FIXMessage.from_json(message)
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Invalid message from GUIBROKER: {e}")
            return

        msg_type = fix_msg.msg_type
        log_recv(logger, "GUIBROKER", f"FIX {msg_type} cl={fix_msg.get(Tag.ClOrdID)} {fix_msg.get(Tag.Symbol)}", message)

        if msg_type == MsgType.NewOrderSingle:
            await self._handle_new_order(websocket, fix_msg)
        elif msg_type == MsgType.OrderCancelRequest:
            await self._handle_cancel_request(websocket, fix_msg)
        elif msg_type == MsgType.OrderCancelReplaceRequest:
            await self._handle_cancel_replace_request(websocket, fix_msg)
        elif msg_type == MsgType.OrderStatusRequest:
            await self._handle_order_status_request(websocket, fix_msg)
        elif msg_type == MsgType.Heartbeat:
            pass  # Heartbeat - no action needed
        else:
            logger.warning(f"Unknown MsgType from GUIBROKER: {msg_type}")

    async def _handle_new_order(self, websocket, fix_msg: FIXMessage):
        """Process a NewOrderSingle from GUIBROKER."""
        cl_ord_id = fix_msg.get(Tag.ClOrdID)

        # Risk checks (acquire lock since _validate_order reads self.orders)
        async with self._orders_lock:
            reject_reason = self._validate_order(fix_msg)
        if reject_reason:
            logger.warning(f"Order rejected [{cl_ord_id}]: {reject_reason}")
            reject = execution_report(
                cl_ord_id=cl_ord_id,
                order_id="NONE",
                exec_type=ExecType.Rejected,
                ord_status=OrdStatus.Rejected,
                symbol=fix_msg.get(Tag.Symbol),
                side=fix_msg.get(Tag.Side),
                leaves_qty=0,
                cum_qty=0,
                avg_px=0,
                text=reject_reason,
            )
            reject_json = reject.to_json()
            log_send(logger, "GUIBROKER", f"FIX Reject cl={cl_ord_id} {reject_reason}", reject_json)
            await self.server.send_to(websocket, reject_json)
            return

        # Assign OM order ID
        order_id = self._next_order_id()
        exchange = self._resolve_exchange(fix_msg)
        try:
            qty = float(fix_msg.get(Tag.OrderQty, "0"))
        except ValueError:
            qty = 0.0
        try:
            price = float(fix_msg.get(Tag.Price, "0"))
        except ValueError:
            price = 0.0

        # Create order book entry
        order = {
            "cl_ord_id": cl_ord_id,
            "order_id": order_id,
            "symbol": fix_msg.get(Tag.Symbol),
            "side": fix_msg.get(Tag.Side),
            "qty": qty,
            "price": price,
            "ord_type": fix_msg.get(Tag.OrdType),
            "exchange": exchange,
            "status": OrdStatus.PendingNew,
            "cum_qty": 0.0,
            "avg_px": 0.0,
            "leaves_qty": qty,
            "source_ws": websocket,
            "created_at": time.time(),
        }
        async with self._orders_lock:
            self.orders[cl_ord_id] = order
            self._om_id_to_cl_ord_id[order_id] = cl_ord_id

        logger.info(
            f"New order accepted: {order_id} cl_ord_id={cl_ord_id} "
            f"{fix_msg.get(Tag.Symbol)} {fix_msg.get(Tag.Side)} "
            f"qty={qty} price={price} exchange={exchange}"
        )

        # Forward to EXCHCONN with OM order ID and resolved exchange
        fix_msg.set(Tag.OrderID, order_id)
        fix_msg.set(Tag.ExDestination, exchange)
        fwd_json = fix_msg.to_json()
        try:
            await self.exchconn_client.send(fwd_json)
            log_send(logger, "EXCHCONN", f"FIX NewOrderSingle cl={cl_ord_id} {fix_msg.get(Tag.Symbol)} -> {exchange}", fwd_json)
        except Exception as e:
            logger.error(f"Failed to forward order {order_id} to EXCHCONN: {e}")
            # Send reject back to GUIBROKER
            order["status"] = OrdStatus.Rejected
            reject = execution_report(
                cl_ord_id=cl_ord_id,
                order_id=order_id,
                exec_type=ExecType.Rejected,
                ord_status=OrdStatus.Rejected,
                symbol=order["symbol"],
                side=order["side"],
                leaves_qty=0,
                cum_qty=0,
                avg_px=0,
                text=f"EXCHCONN communication error: {e}",
            )
            reject_json = reject.to_json()
            log_send(logger, "GUIBROKER", f"FIX Reject cl={cl_ord_id} EXCHCONN error", reject_json)
            await self.server.send_to(websocket, reject_json)

    async def _handle_cancel_request(self, websocket, fix_msg: FIXMessage):
        """Forward cancel request to EXCHCONN."""
        orig_cl_ord_id = fix_msg.get(Tag.OrigClOrdID)
        cl_ord_id = fix_msg.get(Tag.ClOrdID)

        async with self._orders_lock:
            order = self.orders.get(orig_cl_ord_id)
        if not order:
            logger.warning(f"Cancel request for unknown order: {orig_cl_ord_id}")
            reject = execution_report(
                cl_ord_id=cl_ord_id,
                order_id="NONE",
                exec_type=ExecType.Rejected,
                ord_status=OrdStatus.Rejected,
                symbol=fix_msg.get(Tag.Symbol),
                side=fix_msg.get(Tag.Side),
                leaves_qty=0,
                cum_qty=0,
                avg_px=0,
                text=f"Unknown order: {orig_cl_ord_id}",
            )
            reject_json = reject.to_json()
            log_send(logger, "GUIBROKER", f"FIX Reject cancel unknown order {orig_cl_ord_id}", reject_json)
            await self.server.send_to(websocket, reject_json)
            return

        # Map the new ClOrdID back to the original order so we can look it up
        # when the Canceled exec report comes back from EXCHCONN
        async with self._orders_lock:
            if cl_ord_id != orig_cl_ord_id:
                self.orders[cl_ord_id] = order

        # Add OM order ID and forward
        fix_msg.set(Tag.OrderID, order["order_id"])
        cancel_json = fix_msg.to_json()
        log_send(logger, "EXCHCONN", f"FIX CancelRequest cl={cl_ord_id} order={order['order_id']}", cancel_json)
        try:
            await self.exchconn_client.send(cancel_json)
        except Exception as e:
            logger.error(f"Failed to forward cancel for {order['order_id']}: {e}")
            reject = execution_report(
                cl_ord_id=cl_ord_id,
                order_id=order["order_id"],
                exec_type=ExecType.Rejected,
                ord_status=OrdStatus.Rejected,
                symbol=order["symbol"],
                side=order["side"],
                leaves_qty=order["leaves_qty"],
                cum_qty=order["cum_qty"],
                avg_px=order["avg_px"],
                text=f"Cancel forwarding failed: {e}",
            )
            reject_json = reject.to_json()
            log_send(logger, "GUIBROKER", f"FIX Reject cancel forward fail {order['order_id']}", reject_json)
            await self.server.send_to(websocket, reject_json)

    async def _handle_cancel_replace_request(self, websocket, fix_msg: FIXMessage):
        """Forward cancel/replace (amend) request to EXCHCONN."""
        orig_cl_ord_id = fix_msg.get(Tag.OrigClOrdID)
        cl_ord_id = fix_msg.get(Tag.ClOrdID)

        async with self._orders_lock:
            order = self.orders.get(orig_cl_ord_id)
        if not order:
            logger.warning(f"Amend request for unknown order: {orig_cl_ord_id}")
            reject = execution_report(
                cl_ord_id=cl_ord_id,
                order_id="NONE",
                exec_type=ExecType.Rejected,
                ord_status=OrdStatus.Rejected,
                symbol=fix_msg.get(Tag.Symbol),
                side=fix_msg.get(Tag.Side),
                leaves_qty=0,
                cum_qty=0,
                avg_px=0,
                text=f"Unknown order: {orig_cl_ord_id}",
            )
            reject_json = reject.to_json()
            log_send(logger, "GUIBROKER", f"FIX Reject amend unknown order {orig_cl_ord_id}", reject_json)
            await self.server.send_to(websocket, reject_json)
            return

        # Validate new qty/price
        try:
            new_qty = float(fix_msg.get(Tag.OrderQty, "0"))
        except ValueError:
            new_qty = 0
        try:
            new_price = float(fix_msg.get(Tag.Price, "0"))
        except ValueError:
            new_price = 0

        if new_qty <= 0:
            reject = execution_report(
                cl_ord_id=cl_ord_id,
                order_id=order["order_id"],
                exec_type=ExecType.Rejected,
                ord_status=OrdStatus.Rejected,
                symbol=order["symbol"],
                side=order["side"],
                leaves_qty=order["leaves_qty"],
                cum_qty=order["cum_qty"],
                avg_px=order["avg_px"],
                text=f"Invalid amend quantity: {new_qty}",
            )
            reject_json = reject.to_json()
            log_send(logger, "GUIBROKER", f"FIX Reject amend invalid qty={new_qty}", reject_json)
            await self.server.send_to(websocket, reject_json)
            return

        if order["ord_type"] == OrdType.Limit and new_price <= 0:
            reject = execution_report(
                cl_ord_id=cl_ord_id,
                order_id=order["order_id"],
                exec_type=ExecType.Rejected,
                ord_status=OrdStatus.Rejected,
                symbol=order["symbol"],
                side=order["side"],
                leaves_qty=order["leaves_qty"],
                cum_qty=order["cum_qty"],
                avg_px=order["avg_px"],
                text=f"Invalid amend price: {new_price}",
            )
            reject_json = reject.to_json()
            log_send(logger, "GUIBROKER", f"FIX Reject amend invalid price={new_price}", reject_json)
            await self.server.send_to(websocket, reject_json)
            return

        # Risk checks on amended values
        limits = risk_limits.load_limits()
        max_qty_map = limits.get("max_order_qty", {})
        max_qty = max_qty_map.get(order["symbol"])
        if max_qty is not None and new_qty > float(max_qty):
            reject = execution_report(
                cl_ord_id=cl_ord_id,
                order_id=order["order_id"],
                exec_type=ExecType.Rejected,
                ord_status=OrdStatus.Rejected,
                symbol=order["symbol"],
                side=order["side"],
                leaves_qty=order["leaves_qty"],
                cum_qty=order["cum_qty"],
                avg_px=order["avg_px"],
                text=f"Amend qty {new_qty} exceeds max {max_qty} for {order['symbol']}",
            )
            reject_json = reject.to_json()
            log_send(logger, "GUIBROKER", f"FIX Reject amend risk qty={new_qty}", reject_json)
            await self.server.send_to(websocket, reject_json)
            return

        if order["ord_type"] == OrdType.Limit and new_price > 0:
            max_notional = limits.get("max_order_notional")
            if max_notional is not None and new_qty * new_price > float(max_notional):
                reject = execution_report(
                    cl_ord_id=cl_ord_id,
                    order_id=order["order_id"],
                    exec_type=ExecType.Rejected,
                    ord_status=OrdStatus.Rejected,
                    symbol=order["symbol"],
                    side=order["side"],
                    leaves_qty=order["leaves_qty"],
                    cum_qty=order["cum_qty"],
                    avg_px=order["avg_px"],
                    text=f"Amend notional ${new_qty * new_price:,.2f} exceeds max ${float(max_notional):,.2f}",
                )
                reject_json = reject.to_json()
                log_send(logger, "GUIBROKER", f"FIX Reject amend risk notional", reject_json)
                await self.server.send_to(websocket, reject_json)
                return

        # Position limit check — compute net delta (new qty minus old pending qty)
        max_pos_map = limits.get("max_position_qty", {})
        max_pos = max_pos_map.get(order["symbol"])
        if max_pos is not None:
            async with self._positions_lock:
                current_pos = self._positions.get(order["symbol"], 0.0)
            old_leaves = order.get("leaves_qty", 0.0)
            delta = new_qty - old_leaves
            signed_delta = delta if order["side"] == SIDE_BUY else -delta
            projected = current_pos + signed_delta
            if abs(projected) > float(max_pos):
                reject = execution_report(
                    cl_ord_id=cl_ord_id,
                    order_id=order["order_id"],
                    exec_type=ExecType.Rejected,
                    ord_status=OrdStatus.Rejected,
                    symbol=order["symbol"],
                    side=order["side"],
                    leaves_qty=order["leaves_qty"],
                    cum_qty=order["cum_qty"],
                    avg_px=order["avg_px"],
                    text=f"Amend projected position {projected:+.6g} exceeds max {float(max_pos)} for {order['symbol']}",
                )
                reject_json = reject.to_json()
                log_send(logger, "GUIBROKER", f"FIX Reject amend risk position", reject_json)
                await self.server.send_to(websocket, reject_json)
                return

        # Map the new ClOrdID back to the original order so we can look it up
        # when the Replaced exec report comes back from EXCHCONN
        async with self._orders_lock:
            if cl_ord_id != orig_cl_ord_id:
                self.orders[cl_ord_id] = order

        # Forward to EXCHCONN
        fix_msg.set(Tag.OrderID, order["order_id"])
        amend_json = fix_msg.to_json()
        log_send(logger, "EXCHCONN", f"FIX CancelReplaceRequest cl={cl_ord_id} order={order['order_id']}", amend_json)
        try:
            await self.exchconn_client.send(amend_json)
        except Exception as e:
            logger.error(f"Failed to forward amend for {order['order_id']}: {e}")
            reject = execution_report(
                cl_ord_id=cl_ord_id,
                order_id=order["order_id"],
                exec_type=ExecType.Rejected,
                ord_status=OrdStatus.Rejected,
                symbol=order["symbol"],
                side=order["side"],
                leaves_qty=order["leaves_qty"],
                cum_qty=order["cum_qty"],
                avg_px=order["avg_px"],
                text=f"Amend forwarding failed: {e}",
            )
            reject_json = reject.to_json()
            log_send(logger, "GUIBROKER", f"FIX Reject amend forward fail {order['order_id']}", reject_json)
            await self.server.send_to(websocket, reject_json)

    async def _handle_order_status_request(self, websocket, fix_msg: FIXMessage):
        """Return current order status from internal book."""
        cl_ord_id = fix_msg.get(Tag.ClOrdID)
        async with self._orders_lock:
            order = self.orders.get(cl_ord_id)

        if not order:
            reject = execution_report(
                cl_ord_id=cl_ord_id,
                order_id="NONE",
                exec_type=ExecType.Rejected,
                ord_status=OrdStatus.Rejected,
                symbol=fix_msg.get(Tag.Symbol, ""),
                side=fix_msg.get(Tag.Side, ""),
                leaves_qty=0,
                cum_qty=0,
                avg_px=0,
                text=f"Unknown order: {cl_ord_id}",
            )
            await self.server.send_to(websocket, reject.to_json())
            return

        report = execution_report(
            cl_ord_id=order["cl_ord_id"],
            order_id=order["order_id"],
            exec_type=ExecType.New,  # Status response uses current status
            ord_status=order["status"],
            symbol=order["symbol"],
            side=order["side"],
            leaves_qty=order["leaves_qty"],
            cum_qty=order["cum_qty"],
            avg_px=order["avg_px"],
        )
        await self.server.send_to(websocket, report.to_json())

    # ---------------------------------------------------------------
    # EXCHCONN message handling (client side, from port 8084)
    # ---------------------------------------------------------------

    async def _handle_exchconn_message(self, message: str):
        """Process incoming messages from EXCHCONN (execution reports)."""
        try:
            fix_msg = FIXMessage.from_json(message)
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Invalid message from EXCHCONN: {e}")
            return

        msg_type = fix_msg.msg_type
        log_recv(logger, "EXCHCONN", f"FIX {msg_type} cl={fix_msg.get(Tag.ClOrdID)} order={fix_msg.get(Tag.OrderID)}", message)

        if msg_type == MsgType.ExecutionReport:
            await self._handle_execution_report(fix_msg)
        elif msg_type == MsgType.Heartbeat:
            pass
        else:
            logger.warning(f"Unknown MsgType from EXCHCONN: {msg_type}")

    async def _handle_execution_report(self, fix_msg: FIXMessage):
        """Process execution report from EXCHCONN, update order book, forward."""
        cl_ord_id = fix_msg.get(Tag.ClOrdID)
        exec_type = fix_msg.get(Tag.ExecType)
        ord_status = fix_msg.get(Tag.OrdStatus)

        async with self._orders_lock:
            order = self.orders.get(cl_ord_id)
            if not order:
                # Try reverse lookup by OM order ID
                order_id = fix_msg.get(Tag.OrderID)
                cl_ord_id = self._om_id_to_cl_ord_id.get(order_id, "")
                order = self.orders.get(cl_ord_id)

        if not order:
            logger.warning(
                f"Execution report for unknown order: "
                f"cl_ord_id={fix_msg.get(Tag.ClOrdID)} "
                f"order_id={fix_msg.get(Tag.OrderID)}"
            )
            return

        # Update order book
        order["status"] = ord_status

        if exec_type == ExecType.Trade:
            try:
                last_qty = float(fix_msg.get(Tag.LastQty, "0"))
                last_px = float(fix_msg.get(Tag.LastPx, "0"))
                cum_qty = float(fix_msg.get(Tag.CumQty, "0"))
                leaves_qty = float(fix_msg.get(Tag.LeavesQty, "0"))
                avg_px = float(fix_msg.get(Tag.AvgPx, "0"))
            except ValueError:
                last_qty = last_px = cum_qty = leaves_qty = avg_px = 0.0

            order["cum_qty"] = cum_qty
            order["leaves_qty"] = leaves_qty
            order["avg_px"] = avg_px

            logger.info(
                f"Fill on {order['order_id']}: last_qty={last_qty} "
                f"last_px={last_px} cum_qty={cum_qty} leaves_qty={leaves_qty}"
            )

            # Update OM position tracking
            signed_qty = last_qty if order["side"] == SIDE_BUY else -last_qty
            sym = order["symbol"]
            async with self._positions_lock:
                self._positions[sym] = self._positions.get(sym, 0.0) + signed_qty
                net_pos = self._positions[sym]
            logger.info(f"Position update: {sym} {signed_qty:+.6g} -> net {net_pos:+.6g}")

            # Send fill notification to POSMANAGER
            side_str = "BUY" if order["side"] == "1" else "SELL"
            fill_notification = json_msg(
                "fill",
                symbol=order["symbol"],
                side=side_str,
                qty=last_qty,
                price=last_px,
                cl_ord_id=order["cl_ord_id"],
                order_id=order["order_id"],
            )
            try:
                await self.pos_client.send(fill_notification)
                log_send(logger, "POSMANAGER", f"fill {order['symbol']} {side_str} {last_qty}@{last_px}", fill_notification)
            except Exception as e:
                logger.error(f"Failed to send fill to POSMANAGER: {e}")

        elif exec_type == ExecType.Canceled:
            order["leaves_qty"] = 0
            logger.info(f"Order {order['order_id']} canceled")

        elif exec_type == ExecType.Replaced:
            try:
                new_qty = float(fix_msg.get(Tag.OrderQty, str(order["qty"])))
                new_price = float(fix_msg.get(Tag.Price, str(order["price"])))
            except ValueError:
                new_qty = order["qty"]
                new_price = order["price"]
            order["qty"] = new_qty
            order["price"] = new_price
            order["leaves_qty"] = new_qty - order["cum_qty"]
            logger.info(
                f"Order {order['order_id']} replaced: "
                f"qty={new_qty} price={new_price}"
            )

        elif exec_type == ExecType.New:
            order["status"] = OrdStatus.New
            logger.info(f"Order {order['order_id']} acknowledged (New)")

        elif exec_type == ExecType.Rejected:
            order["status"] = OrdStatus.Rejected
            order["leaves_qty"] = 0
            logger.warning(
                f"Order {order['order_id']} rejected by exchange: "
                f"{fix_msg.get(Tag.Text)}"
            )

        # Forward execution report to GUIBROKER
        source_ws = order.get("source_ws")
        fwd_json = fix_msg.to_json()
        if source_ws:
            try:
                await self.server.send_to(source_ws, fwd_json)
                log_send(logger, "GUIBROKER", f"FIX ExecReport {exec_type} order={order['order_id']}", fwd_json)
            except Exception as e:
                logger.error(f"Failed to forward exec report to GUIBROKER: {e}")
        else:
            logger.warning(f"No source WS for order {order['order_id']}, cannot forward")

        # Schedule cleanup of terminal orders to prevent unbounded growth
        if ord_status in (OrdStatus.Filled, OrdStatus.Canceled, OrdStatus.Rejected):
            asyncio.get_running_loop().call_later(
                60, lambda cid=cl_ord_id, oid=order["order_id"]: asyncio.ensure_future(
                    self._cleanup_terminal_order(cid, oid)
                )
            )

    async def _cleanup_terminal_order(self, cl_ord_id: str, order_id: str):
        """Remove a terminal order from internal tracking after a delay."""
        async with self._orders_lock:
            order = self.orders.get(cl_ord_id)
            if order and order.get("status") in (OrdStatus.Filled, OrdStatus.Canceled, OrdStatus.Rejected):
                del self.orders[cl_ord_id]
                self._om_id_to_cl_ord_id.pop(order_id, None)
                logger.debug(f"Cleaned up terminal order: {order_id} (cl={cl_ord_id})")

    # ---------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------

    async def start(self):
        """Start the Order Manager."""
        logger.info("=" * 60)
        logger.info("Order Manager (OM) starting...")
        logger.info(f"  Server port: {PORTS['OM']} (GUIBROKER connects here)")
        logger.info(f"  EXCHCONN:    ws://{HOST}:{PORTS['EXCHCONN']}")
        logger.info(f"  POSMANAGER:  ws://{HOST}:{PORTS['POSMANAGER']}")
        logger.info("=" * 60)

        # Start WS server for GUIBROKER
        await self.server.start()

        # Connect to EXCHCONN and POSMANAGER (with retry)
        await asyncio.gather(
            self.exchconn_client.connect(retry=True),
            self.pos_client.connect(retry=True),
        )

        # Listen on both client connections
        await asyncio.gather(
            self.exchconn_client.listen(),
            self.pos_client.listen(),
        )

    async def shutdown(self):
        """Gracefully shut down the Order Manager."""
        logger.info("Order Manager shutting down...")
        await self.exchconn_client.close()
        await self.pos_client.close()
        await self.server.stop()
        logger.info("Order Manager stopped.")


async def main():
    om = OrderManager()

    loop = asyncio.get_running_loop()

    def _signal_handler():
        logger.info("Received shutdown signal")
        task = asyncio.ensure_future(om.shutdown())
        task.add_done_callback(
            lambda t: logger.error(f"Shutdown error: {t.exception()}") if t.exception() else None
        )

    # Register signal handlers (Unix-compatible; on Windows SIGINT works)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler for all signals
            signal.signal(sig, lambda s, f: _signal_handler())

    try:
        await om.start()
    except KeyboardInterrupt:
        await om.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
