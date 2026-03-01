"""
Exchange Connector (EXCHCONN) - Main server component.

Runs a WebSocket server on port 8084 that accepts FIX messages from the
Order Manager (OM), routes them to the appropriate simulated exchange adapter
(Binance or Coinbase) based on ExDestination, and forwards execution reports
back to OM.

Run with: python -m exchconn.exchconn
"""

import asyncio
import collections
import logging
import signal
import sys

from shared.config import HOST, PORTS, EXCHANGES, DEFAULT_ROUTING, USE_REAL_COINBASE, USE_COINBASE_FIX
from shared.fix_protocol import FIXMessage, Tag, MsgType, ExecType, OrdStatus
from shared.logging_config import setup_component_logging, log_recv, log_send
from shared.ws_transport import WSServer

from exchconn.binance_sim import BinanceSimulator

if USE_COINBASE_FIX:
    from exchconn.coinbase_fix_adapter import CoinbaseFIXAdapter as CoinbaseExchange
elif USE_REAL_COINBASE:
    from exchconn.coinbase_adapter import CoinbaseAdapter as CoinbaseExchange
else:
    from exchconn.coinbase_sim import CoinbaseSimulator as CoinbaseExchange

# Defense-in-depth hard safety limit — reject orders exceeding this qty
MAX_ORDER_QTY = 1000

logger = setup_component_logging("EXCHCONN")


class ExchangeConnector:
    """
    Main EXCHCONN component.

    Accepts FIX messages from OM on a WebSocket server, routes orders to
    simulated exchange adapters, and forwards execution reports back.
    """

    def __init__(self):
        self._server = WSServer(HOST, PORTS["EXCHCONN"], name="EXCHCONN")
        self._server.on_message(self._handle_message)
        self._server.on_connect(self._handle_connect)
        self._server.on_disconnect(self._handle_disconnect)

        # Initialize exchange simulators
        self._exchanges = {
            "BINANCE": BinanceSimulator(),
            "COINBASE": CoinbaseExchange(),
        }

        # Set report callbacks so exchanges can send reports back through us
        for name, exchange in self._exchanges.items():
            exchange.set_report_callback(self._on_execution_report)

        self._om_clients = set()  # Track connected OM clients
        self._pending_reports = collections.deque(maxlen=1000)  # Queue for reports when OM disconnected

    async def _handle_connect(self, websocket):
        """Handle a new client connection (OM connecting)."""
        self._om_clients.add(websocket)
        logger.info(f"OM client connected. Total clients: {len(self._om_clients)}")
        # Replay any queued exec reports
        replayed = 0
        while self._pending_reports:
            report_json = self._pending_reports.popleft()
            try:
                await self._server.send_to(websocket, report_json)
                replayed += 1
            except Exception as e:
                logger.error(f"Failed to replay queued report: {e}")
                break
        if replayed:
            logger.info(f"Replayed {replayed} queued execution reports to reconnected OM")

    async def _handle_disconnect(self, websocket):
        """Handle client disconnection."""
        self._om_clients.discard(websocket)
        logger.info(f"OM client disconnected. Total clients: {len(self._om_clients)}")

    async def _handle_message(self, websocket, raw_message: str):
        """Handle incoming FIX message from OM."""
        try:
            fix_msg = FIXMessage.from_json(raw_message)
            msg_type = fix_msg.msg_type

            if msg_type == MsgType.Heartbeat:
                return

            log_recv(logger, "OM", f"FIX {msg_type} cl={fix_msg.get(Tag.ClOrdID)} {fix_msg.get(Tag.Symbol)} -> {fix_msg.get(Tag.ExDestination)}", raw_message)

            # Determine which exchange to route to
            exchange_name = fix_msg.get(Tag.ExDestination)
            if not exchange_name:
                # Use default routing based on symbol
                symbol = fix_msg.get(Tag.Symbol)
                exchange_name = DEFAULT_ROUTING.get(symbol, "BINANCE")
                fix_msg.set(Tag.ExDestination, exchange_name)

            exchange = self._exchanges.get(exchange_name)
            if not exchange:
                logger.error(f"Unknown exchange: {exchange_name}")
                await self._send_reject(
                    websocket, fix_msg, f"Unknown exchange: {exchange_name}"
                )
                return

            # Route based on message type
            if msg_type == MsgType.NewOrderSingle:
                # Defense-in-depth: basic sanity check on qty
                try:
                    qty = float(fix_msg.get(Tag.OrderQty, "0"))
                except (ValueError, TypeError):
                    qty = 0.0
                if qty <= 0:
                    logger.warning(f"Sanity check failed: qty={qty} <= 0 for cl={fix_msg.get(Tag.ClOrdID)}")
                    await self._send_reject(websocket, fix_msg, f"Invalid quantity: {qty}")
                    return
                if qty > MAX_ORDER_QTY:
                    logger.warning(f"Sanity check failed: qty={qty} > max {MAX_ORDER_QTY} for cl={fix_msg.get(Tag.ClOrdID)}")
                    await self._send_reject(websocket, fix_msg, f"Quantity {qty} exceeds safety limit of {MAX_ORDER_QTY}")
                    return
                await exchange.submit_order(fix_msg)
            elif msg_type == MsgType.OrderCancelRequest:
                await exchange.cancel_order(fix_msg)
            elif msg_type == MsgType.OrderCancelReplaceRequest:
                await exchange.amend_order(fix_msg)
            else:
                logger.warning(f"Unhandled message type: {msg_type}")

        except Exception as e:
            logger.error(f"Error processing message: {e}", exc_info=True)

    async def _on_execution_report(self, report: FIXMessage):
        """Callback from exchange simulators - forward execution report to all OM clients."""
        report_json = report.to_json()
        desc = f"FIX ExecReport {report.get(Tag.ExecType)} order={report.get(Tag.OrderID)}"
        log_send(logger, "OM", desc, report_json)
        # Send to all connected OM clients, or queue if none connected
        if not self._om_clients:
            self._pending_reports.append(report_json)
            logger.warning(f"No OM clients connected, queued exec report (queue size: {len(self._pending_reports)})")
            return
        for client in list(self._om_clients):
            try:
                await self._server.send_to(client, report_json)
            except Exception as e:
                logger.error(f"Failed to send report to OM client: {e}")

    async def _send_reject(self, websocket, orig_msg: FIXMessage, reason: str):
        """Send a reject execution report for an unroutable order."""
        from shared.fix_protocol import execution_report

        reject = execution_report(
            cl_ord_id=orig_msg.get(Tag.ClOrdID),
            order_id="NONE",
            exec_type=ExecType.Rejected,
            ord_status=OrdStatus.Rejected,
            symbol=orig_msg.get(Tag.Symbol),
            side=orig_msg.get(Tag.Side),
            leaves_qty=0.0,
            cum_qty=0.0,
            avg_px=0.0,
            text=reason,
        )
        reject_json = reject.to_json()
        log_send(logger, "OM", f"FIX Reject cl={orig_msg.get(Tag.ClOrdID)} {reason}", reject_json)
        await self._server.send_to(websocket, reject_json)

    async def start(self):
        """Start the EXCHCONN server and all exchange simulators."""
        logger.info("Starting Exchange Connector...")

        # Start exchange simulators
        for name, exchange in self._exchanges.items():
            await exchange.start()

        # Start the WebSocket server
        await self._server.start()
        logger.info(
            f"EXCHCONN ready on ws://{HOST}:{PORTS['EXCHCONN']} "
            f"with exchanges: {list(self._exchanges.keys())}"
        )

    async def stop(self):
        """Stop the server and all exchange simulators."""
        logger.info("Stopping Exchange Connector...")
        for name, exchange in self._exchanges.items():
            await exchange.stop()
        await self._server.stop()
        logger.info("Exchange Connector stopped.")


async def main():
    """Entry point for EXCHCONN component."""
    connector = ExchangeConnector()
    await connector.start()

    # Wait until interrupted
    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler for SIGTERM
            pass

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        logger.warning("KeyboardInterrupt received during stop_event.wait()")
    finally:
        await connector.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.warning("KeyboardInterrupt received, shutting down EXCHCONN")
