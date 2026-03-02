"""
GUIBROKER - Bridges the GUI (JSON) and Order Manager (FIX protocol).

Runs a WebSocket server on port 8082 for GUI clients and connects as a
WebSocket client to the Order Manager on ws://localhost:8083. Converts
JSON order messages from the GUI to FIX protocol and FIX ExecutionReports
from the OM back to JSON for the GUI.

Run with: python -m guibroker.guibroker
"""

import asyncio
import itertools
import json
import logging
import signal
import sys
from collections import deque
from typing import Optional

from websockets.asyncio.server import ServerConnection

from shared.config import PORTS, HOST, ws_url
from shared.fix_protocol import (
    FIXMessage,
    Tag,
    MsgType,
    ExecType,
    OrdStatus,
    Side,
    OrdType,
    new_order_single,
    cancel_request,
    cancel_replace_request,
)
from shared.logging_config import setup_component_logging, log_recv, log_send
from shared.ws_transport import WSServer, WSClient, parse_json_msg

logger = setup_component_logging("GUIBROKER")

# Maps for converting between human-readable strings and FIX enum values
SIDE_MAP = {"BUY": Side.Buy, "SELL": Side.Sell}
SIDE_REVERSE = {Side.Buy: "BUY", Side.Sell: "SELL"}

ORD_TYPE_MAP = {"MARKET": OrdType.Market, "LIMIT": OrdType.Limit}

EXEC_TYPE_REVERSE = {
    ExecType.New: "NEW",
    ExecType.Canceled: "CANCELED",
    ExecType.Replaced: "REPLACED",
    ExecType.Rejected: "REJECTED",
    ExecType.Trade: "TRADE",
    ExecType.PendingNew: "PENDING_NEW",
}

ORD_STATUS_REVERSE = {
    OrdStatus.New: "NEW",
    OrdStatus.PartiallyFilled: "PARTIALLY_FILLED",
    OrdStatus.Filled: "FILLED",
    OrdStatus.Canceled: "CANCELED",
    OrdStatus.Replaced: "REPLACED",
    OrdStatus.PendingNew: "PENDING_NEW",
    OrdStatus.Rejected: "REJECTED",
}


class GUIBroker:
    """Bridges GUI clients (JSON over WS) and the Order Manager (FIX over WS)."""

    def __init__(self):
        self._order_counter = itertools.count(1)
        # ClOrdID -> GUI websocket that sent the order
        self._client_orders: dict[str, ServerConnection] = {}
        # Map cancel/amend ClOrdIDs back to original order ClOrdID for GUI
        self._cancel_to_orig: dict[str, str] = {}
        # Queue for messages when OM is not connected
        self._pending_queue: deque[str] = deque()
        self._om_connected = False
        self._om_lock = asyncio.Lock()

        # WS server for GUI clients on port 8082
        self._gui_server = WSServer(HOST, PORTS["GUIBROKER"], name="GUIBROKER-Server")
        self._gui_server.on_message(self._handle_gui_message)
        self._gui_server.on_connect(self._handle_gui_connect)
        self._gui_server.on_disconnect(self._handle_gui_disconnect)

        # WS client to connect to OM on port 8083
        self._om_client = WSClient(ws_url("OM"), name="GUIBROKER->OM")
        self._om_client.on_message(self._handle_om_message)

    def _next_cl_ord_id(self) -> str:
        n = next(self._order_counter)
        return f"GUI-{n}"

    # -------------------------------------------------------------------------
    # GUI-side handlers (JSON from GUI clients)
    # -------------------------------------------------------------------------

    async def _handle_gui_connect(self, websocket: ServerConnection):
        logger.info(f"GUI client connected: {websocket.remote_address}")

    async def _handle_gui_disconnect(self, websocket: ServerConnection):
        logger.info(f"GUI client disconnected: {websocket.remote_address}")
        # Clean up any order mappings for this client
        stale_ids = [
            cid for cid, ws in self._client_orders.items() if ws is websocket
        ]
        for cid in stale_ids:
            del self._client_orders[cid]
        if stale_ids:
            logger.info(f"Cleaned up {len(stale_ids)} order mappings for disconnected GUI client")

    async def _handle_gui_message(self, websocket: ServerConnection, raw: str):
        try:
            msg = parse_json_msg(raw)
        except json.JSONDecodeError:
            logger.error(f"Malformed JSON from GUI: {raw[:200]}")
            await self._send_error_to_gui(websocket, "Invalid JSON message")
            return

        msg_type = msg.get("type")
        log_recv(logger, "GUI", f"{msg_type} {msg.get('symbol', '')} {msg.get('side', '')}", raw)

        try:
            if msg_type == "new_order":
                await self._handle_new_order(websocket, msg)
            elif msg_type == "cancel_order":
                await self._handle_cancel_order(websocket, msg)
            elif msg_type == "amend_order":
                await self._handle_amend_order(websocket, msg)
            else:
                logger.warning(f"Unknown message type from GUI: {msg_type}")
                await self._send_error_to_gui(websocket, f"Unknown message type: {msg_type}")
        except Exception as e:
            logger.error(f"Error processing GUI message: {e}", exc_info=True)
            await self._send_error_to_gui(websocket, f"Processing error: {e}")

    async def _handle_new_order(self, websocket: ServerConnection, msg: dict):
        cl_ord_id = self._next_cl_ord_id()
        symbol = msg.get("symbol", "")
        side_str = msg.get("side", "BUY").upper()
        try:
            qty = float(msg.get("qty", 0))
        except (ValueError, TypeError):
            await self._send_error_to_gui(websocket, f"Invalid quantity: {msg.get('qty')}")
            return
        ord_type_str = msg.get("ord_type", "LIMIT").upper()
        try:
            price = float(msg.get("price", 0))
        except (ValueError, TypeError):
            await self._send_error_to_gui(websocket, f"Invalid price: {msg.get('price')}")
            return
        exchange = msg.get("exchange", "")

        side = SIDE_MAP.get(side_str, Side.Buy)
        ord_type = ORD_TYPE_MAP.get(ord_type_str, OrdType.Limit)

        # Don't send "AUTO" as exchange - let OM use default routing
        if exchange == "AUTO":
            exchange = ""

        fix_msg = new_order_single(
            cl_ord_id=cl_ord_id,
            symbol=symbol,
            side=side,
            qty=qty,
            ord_type=ord_type,
            price=price,
            exchange=exchange,
        )

        # Track which GUI client sent this order
        self._client_orders[cl_ord_id] = websocket
        logger.info(f"NewOrderSingle: cl_ord_id={cl_ord_id} {side_str} {qty} {symbol} @ {price} on {exchange}")
        logger.debug(f"FIX out: {fix_msg}")

        # Send acknowledgment back to GUI with assigned ClOrdID
        ack = json.dumps({
            "type": "order_ack",
            "cl_ord_id": cl_ord_id,
            "symbol": symbol,
            "side": side_str,
            "qty": qty,
            "price": price,
        })
        await self._gui_server.send_to(websocket, ack)
        log_send(logger, "GUI", f"order_ack cl={cl_ord_id} {symbol}", ack)

        fix_json = fix_msg.to_json()
        log_send(logger, "OM", f"FIX {MsgType.NewOrderSingle} cl={cl_ord_id} {symbol} {side_str}", fix_json)
        await self._send_to_om(fix_json)

    async def _handle_cancel_order(self, websocket: ServerConnection, msg: dict):
        new_cl_ord_id = self._next_cl_ord_id()
        orig_cl_ord_id = msg.get("cl_ord_id", "")
        symbol = msg.get("symbol", "")
        side_str = msg.get("side", "BUY").upper()
        side = SIDE_MAP.get(side_str, Side.Buy)

        fix_msg = cancel_request(
            cl_ord_id=new_cl_ord_id,
            orig_cl_ord_id=orig_cl_ord_id,
            symbol=symbol,
            side=side,
        )

        # Map the new ClOrdID to the same GUI client and track the original
        self._client_orders[new_cl_ord_id] = websocket
        self._cancel_to_orig[new_cl_ord_id] = orig_cl_ord_id
        logger.info(f"CancelRequest: new_id={new_cl_ord_id} orig_id={orig_cl_ord_id} {symbol}")

        fix_json = fix_msg.to_json()
        log_send(logger, "OM", f"FIX {MsgType.OrderCancelRequest} cl={new_cl_ord_id} {symbol}", fix_json)
        await self._send_to_om(fix_json)

    async def _handle_amend_order(self, websocket: ServerConnection, msg: dict):
        new_cl_ord_id = self._next_cl_ord_id()
        orig_cl_ord_id = msg.get("cl_ord_id", "")
        symbol = msg.get("symbol", "")
        side_str = msg.get("side", "BUY").upper()
        side = SIDE_MAP.get(side_str, Side.Buy)
        try:
            qty = float(msg.get("qty", 0))
        except (ValueError, TypeError):
            await self._send_error_to_gui(websocket, f"Invalid amend quantity: {msg.get('qty')}")
            return
        try:
            price = float(msg.get("price", 0))
        except (ValueError, TypeError):
            await self._send_error_to_gui(websocket, f"Invalid amend price: {msg.get('price')}")
            return

        fix_msg = cancel_replace_request(
            cl_ord_id=new_cl_ord_id,
            orig_cl_ord_id=orig_cl_ord_id,
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
        )

        self._client_orders[new_cl_ord_id] = websocket
        self._cancel_to_orig[new_cl_ord_id] = orig_cl_ord_id
        logger.info(
            f"CancelReplaceRequest: new_id={new_cl_ord_id} orig_id={orig_cl_ord_id} "
            f"{symbol} qty={qty} px={price}"
        )

        fix_json = fix_msg.to_json()
        log_send(logger, "OM", f"FIX {MsgType.OrderCancelReplaceRequest} cl={new_cl_ord_id} {symbol}", fix_json)
        await self._send_to_om(fix_json)

    # -------------------------------------------------------------------------
    # OM-side handler (FIX from Order Manager)
    # -------------------------------------------------------------------------

    async def _handle_om_message(self, raw: str):
        try:
            fix_msg = FIXMessage.from_json(raw)
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Malformed FIX message from OM: {e} - {raw[:200]}")
            return

        msg_type = fix_msg.msg_type
        log_recv(logger, "OM", f"FIX {msg_type} cl={fix_msg.get(Tag.ClOrdID)}", raw)

        if msg_type == MsgType.ExecutionReport:
            await self._handle_execution_report(fix_msg)
        elif msg_type == MsgType.Heartbeat:
            logger.debug("Heartbeat from OM")
        else:
            logger.warning(f"Unhandled FIX MsgType from OM: {msg_type}")

    async def _handle_execution_report(self, fix_msg: FIXMessage):
        cl_ord_id = fix_msg.get(Tag.ClOrdID)
        order_id = fix_msg.get(Tag.OrderID)
        exec_type_raw = fix_msg.get(Tag.ExecType)
        ord_status_raw = fix_msg.get(Tag.OrdStatus)
        symbol = fix_msg.get(Tag.Symbol)
        side_raw = fix_msg.get(Tag.Side)

        # For cancel/amend responses, map back to the original order's cl_ord_id
        # so the GUI can update the correct row in the blotter
        gui_cl_ord_id = self._cancel_to_orig.get(cl_ord_id, cl_ord_id)

        # Convert numeric FIX fields to float for clean JSON serialization
        def _safe_float(val, default=0.0):
            try:
                return float(val)
            except (ValueError, TypeError):
                return default

        json_report = {
            "type": "execution_report",
            "cl_ord_id": gui_cl_ord_id,
            "order_id": order_id,
            "exec_type": EXEC_TYPE_REVERSE.get(exec_type_raw, exec_type_raw),
            "symbol": symbol,
            "side": SIDE_REVERSE.get(side_raw, side_raw),
            "status": ORD_STATUS_REVERSE.get(ord_status_raw, ord_status_raw),
            "qty": _safe_float(fix_msg.get(Tag.OrderQty, "0")),
            "filled_qty": _safe_float(fix_msg.get(Tag.CumQty, "0")),
            "avg_px": _safe_float(fix_msg.get(Tag.AvgPx, "0")),
            "leaves_qty": _safe_float(fix_msg.get(Tag.LeavesQty, "0")),
            "last_px": _safe_float(fix_msg.get(Tag.LastPx, "0")),
            "last_qty": _safe_float(fix_msg.get(Tag.LastQty, "0")),
            "text": fix_msg.get(Tag.Text, ""),
            "exchange": fix_msg.get(Tag.ExDestination, ""),
        }

        logger.info(
            f"ExecReport: cl_ord_id={cl_ord_id} order_id={order_id} "
            f"exec_type={json_report['exec_type']} status={json_report['status']} "
            f"symbol={symbol}"
        )

        report_str = json.dumps(json_report)

        # Route the execution report to the correct GUI client
        target_ws = self._client_orders.get(cl_ord_id)
        if target_ws:
            try:
                await self._gui_server.send_to(target_ws, report_str)
                log_send(logger, "GUI", f"execution_report cl={gui_cl_ord_id} {json_report['exec_type']} {symbol}", report_str)
            except Exception as e:
                logger.error(f"Failed to send exec report to GUI client: {e}")
        else:
            # If we don't have a mapping, broadcast to all connected GUI clients
            logger.warning(
                f"No GUI client mapping for cl_ord_id={cl_ord_id}, broadcasting to all"
            )
            await self._gui_server.broadcast(report_str)
            log_send(logger, "GUI(broadcast)", f"execution_report cl={gui_cl_ord_id} {json_report['exec_type']} {symbol}", report_str)

    # -------------------------------------------------------------------------
    # OM communication helpers
    # -------------------------------------------------------------------------

    async def _send_to_om(self, message: str):
        async with self._om_lock:
            if self._om_connected and self._om_client.is_connected:
                try:
                    await self._om_client.send(message)
                    logger.debug(f"Sent to OM: {message[:100]}")
                except Exception as e:
                    logger.error(f"Failed to send to OM: {e}")
                    self._om_connected = False
                    self._pending_queue.append(message)
                    logger.info(f"Queued message for OM (queue size: {len(self._pending_queue)})")
            else:
                self._pending_queue.append(message)
                logger.info(f"OM not connected. Queued message (queue size: {len(self._pending_queue)})")

    async def _flush_pending_queue(self):
        async with self._om_lock:
            while self._pending_queue and self._om_connected:
                message = self._pending_queue.popleft()
                try:
                    await self._om_client.send(message)
                    logger.info(f"Flushed queued message to OM")
                except Exception as e:
                    logger.error(f"Failed to flush message to OM: {e}")
                    self._pending_queue.appendleft(message)
                    self._om_connected = False
                    break

    async def _send_error_to_gui(self, websocket: ServerConnection, error: str):
        msg = json.dumps({"type": "error", "message": error})
        try:
            await self._gui_server.send_to(websocket, msg)
        except Exception as e:
            logger.warning(f"Failed to send error to GUI client: {e}")

    # -------------------------------------------------------------------------
    # Main run loop
    # -------------------------------------------------------------------------

    async def _om_connect_and_listen(self):
        """Connect to OM and listen for messages, with automatic reconnection."""
        while True:
            try:
                await self._om_client.connect(retry=True)
                async with self._om_lock:
                    self._om_connected = True
                logger.info("Connected to OM - flushing pending queue")
                await self._flush_pending_queue()
                await self._om_client.listen()
            except Exception as e:
                logger.error(f"OM connection error: {e}")
            finally:
                async with self._om_lock:
                    self._om_connected = False
                await self._om_client.close()
                logger.warning("Lost connection to OM, will reconnect...")
                await asyncio.sleep(2)

    async def run(self):
        logger.info("=" * 60)
        logger.info("GUIBROKER starting")
        logger.info(f"  GUI server: ws://{HOST}:{PORTS['GUIBROKER']}")
        logger.info(f"  OM client:  {ws_url('OM')}")
        logger.info("=" * 60)

        # Start the GUI WebSocket server
        await self._gui_server.start()

        # Connect to OM and listen (runs forever with reconnect)
        try:
            await self._om_connect_and_listen()
        except asyncio.CancelledError:
            logger.info("GUIBROKER shutting down...")
        finally:
            await self._om_client.close()
            await self._gui_server.stop()
            logger.info("GUIBROKER stopped")


async def main():
    broker = GUIBroker()

    loop = asyncio.get_running_loop()

    # Graceful shutdown on SIGINT/SIGTERM (Unix only)
    if sys.platform != "win32":
        def _signal_handler():
            task = asyncio.ensure_future(_shutdown(loop))
            task.add_done_callback(
                lambda t: logger.error(f"Shutdown error: {t.exception()}")
                if not t.cancelled() and t.exception() else None
            )
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

    await broker.run()


async def _shutdown(loop):
    logger.info("Received shutdown signal")
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    loop.call_soon(loop.stop)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("GUIBROKER terminated by user")
