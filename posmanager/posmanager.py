"""
Position Manager (POSMANAGER) - Tracks real-time positions based on fills
and broadcasts position updates to GUI clients.

Runs WS server on port 8085 (GUI connects here, OM sends fills here).
Connects as WS client to MKTDATA on ws://localhost:8081 for market prices.

Run with: python -m posmanager.posmanager
"""

import asyncio
import json
import logging
import time
import signal

from shared.config import PORTS, HOST, ws_url
from shared.logging_config import setup_component_logging, log_recv, log_send
from shared.ws_transport import WSServer, WSClient, json_msg, parse_json_msg

logger = setup_component_logging("POSMANAGER")


class Position:
    """Tracks a single symbol's position state."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.qty = 0.0            # positive = long, negative = short
        self.avg_cost = 0.0       # weighted average cost basis
        self.market_price = 0.0   # latest from MKTDATA
        self.realized_pnl = 0.0   # accumulated from closed trades

    @property
    def unrealized_pnl(self) -> float:
        if self.qty == 0.0 or self.market_price == 0.0:
            return 0.0
        return (self.market_price - self.avg_cost) * self.qty

    def apply_fill(self, side: str, fill_qty: float, fill_price: float):
        """Apply a fill to this position, updating qty, avg_cost, and realized P&L.

        Side is "BUY" or "SELL".
        Position math handles crossing from long to short and vice versa.
        """
        if fill_qty <= 0:
            logger.warning(f"[{self.symbol}] Ignoring fill with qty <= 0: {fill_qty}")
            return

        if side == "BUY":
            self._apply_buy(fill_qty, fill_price)
        elif side == "SELL":
            self._apply_sell(fill_qty, fill_price)
        else:
            logger.error(f"[{self.symbol}] Unknown side: {side}")

    def _apply_buy(self, fill_qty: float, fill_price: float):
        """Process a BUY fill."""
        if self.qty >= 0:
            # Currently flat or long -- add to long position
            self._add_to_position(fill_qty, fill_price)
        else:
            # Currently short -- close some/all of the short, possibly flip to long
            close_qty = min(fill_qty, abs(self.qty))
            # Realized P&L on closing short: (avg_cost - fill_price) * close_qty
            self.realized_pnl += (self.avg_cost - fill_price) * close_qty
            logger.info(
                f"[{self.symbol}] Closed {close_qty} short @ {fill_price}, "
                f"realized P&L delta: {(self.avg_cost - fill_price) * close_qty:.4f}"
            )

            remainder = fill_qty - close_qty
            self.qty += close_qty  # move qty toward zero

            if remainder > 0:
                # Flipping from short to long -- reset avg_cost for new long
                self.qty = 0.0
                self.avg_cost = 0.0
                self._add_to_position(remainder, fill_price)
            elif self.qty == 0.0:
                # Fully closed the short
                self.avg_cost = 0.0

    def _apply_sell(self, fill_qty: float, fill_price: float):
        """Process a SELL fill."""
        if self.qty <= 0:
            # Currently flat or short -- add to short position
            self._add_to_short(fill_qty, fill_price)
        else:
            # Currently long -- close some/all of the long, possibly flip to short
            close_qty = min(fill_qty, self.qty)
            # Realized P&L on closing long: (fill_price - avg_cost) * close_qty
            self.realized_pnl += (fill_price - self.avg_cost) * close_qty
            logger.info(
                f"[{self.symbol}] Closed {close_qty} long @ {fill_price}, "
                f"realized P&L delta: {(fill_price - self.avg_cost) * close_qty:.4f}"
            )

            remainder = fill_qty - close_qty
            self.qty -= close_qty  # move qty toward zero

            if remainder > 0:
                # Flipping from long to short -- reset avg_cost for new short
                self.qty = 0.0
                self.avg_cost = 0.0
                self._add_to_short(remainder, fill_price)
            elif self.qty == 0.0:
                # Fully closed the long
                self.avg_cost = 0.0

    def _add_to_position(self, fill_qty: float, fill_price: float):
        """Add to a long position (or open a new one from flat)."""
        if self.qty == 0.0:
            self.avg_cost = fill_price
            self.qty = fill_qty
        else:
            total_cost = self.avg_cost * self.qty + fill_price * fill_qty
            self.qty += fill_qty
            self.avg_cost = total_cost / self.qty

    def _add_to_short(self, fill_qty: float, fill_price: float):
        """Add to a short position (or open a new one from flat)."""
        abs_qty = abs(self.qty)
        if abs_qty == 0.0:
            self.avg_cost = fill_price
            self.qty = -fill_qty
        else:
            total_cost = self.avg_cost * abs_qty + fill_price * fill_qty
            abs_qty += fill_qty
            self.avg_cost = total_cost / abs_qty
            self.qty = -abs_qty

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "qty": round(self.qty, 8),
            "avg_cost": round(self.avg_cost, 8),
            "market_price": round(self.market_price, 8),
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "realized_pnl": round(self.realized_pnl, 4),
        }


class PositionManager:
    """Main POSMANAGER component."""

    def __init__(self):
        # Per-symbol position tracking
        self.positions: dict[str, Position] = {}

        # WS server on port 8085 for GUI and OM connections
        self.server = WSServer(HOST, PORTS["POSMANAGER"], name="POSMANAGER")
        self.server.on_message(self._handle_server_message)
        self.server.on_connect(self._handle_client_connect)

        # WS client to MKTDATA on port 8081 for market prices
        self.mktdata_client = WSClient(ws_url("MKTDATA"), name="POSMANAGER->MKTDATA")
        self.mktdata_client.on_message(self._handle_mktdata_message)

        # Broadcast throttling: max 2 per second
        self._last_broadcast_time = 0.0
        self._broadcast_interval = 0.5  # 500ms = max 2/sec
        self._broadcast_pending = False
        self._broadcast_lock = asyncio.Lock()

        # Background task reference (Task #6: store create_task result)
        self._mktdata_task: asyncio.Task | None = None

        # Fill sequence tracking for drift detection (Task #7)
        self._fill_sequence = 0

    def _get_position(self, symbol: str) -> Position:
        if symbol not in self.positions:
            self.positions[symbol] = Position(symbol)
        return self.positions[symbol]

    async def _handle_server_message(self, websocket, message: str):
        """Handle incoming messages on the WS server (fills from OM, or queries from GUI)."""
        try:
            data = parse_json_msg(message)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON received: {message[:100]}")
            return

        msg_type = data.get("type")

        if msg_type == "fill":
            log_recv(logger, "OM", f"fill {data.get('symbol')} {data.get('side')} {data.get('qty')}@{data.get('price')}", message)
            await self._process_fill(data)
        elif msg_type == "get_positions":
            log_recv(logger, "GUI", "get_positions", message)
            # GUI requesting current positions snapshot
            update = self._build_position_update()
            await self.server.send_to(websocket, update)
            log_send(logger, "CLIENT", f"position_update {len(self.positions)} positions", update)
        else:
            logger.debug(f"Unhandled message type: {msg_type}")

    async def _handle_client_connect(self, websocket):
        """When a new client connects, send them the current positions."""
        update = self._build_position_update()
        await self.server.send_to(websocket, update)

    async def _process_fill(self, data: dict):
        """Process a fill message and update position state."""
        symbol = data.get("symbol", "")
        side = data.get("side", "")
        qty = float(data.get("qty", 0))
        price = float(data.get("price", 0))
        cl_ord_id = data.get("cl_ord_id", "")
        order_id = data.get("order_id", "")

        if not symbol or not side or qty <= 0 or price <= 0:
            logger.warning(f"Invalid fill data: {data}")
            return

        logger.info(
            f"Fill received: {side} {qty} {symbol} @ {price} "
            f"(cl_ord_id={cl_ord_id}, order_id={order_id})"
        )

        pos = self._get_position(symbol)
        pos.apply_fill(side, qty, price)

        # Track fill sequence for drift detection
        self._fill_sequence += 1
        if self._fill_sequence % 100 == 0:
            logger.info(
                f"Fill sequence milestone: {self._fill_sequence} fills processed. "
                f"Active positions: {sum(1 for p in self.positions.values() if p.qty != 0)}"
            )

        logger.info(
            f"Position updated: {symbol} qty={pos.qty:.8f} avg_cost={pos.avg_cost:.4f} "
            f"realized_pnl={pos.realized_pnl:.4f}"
        )

        await self._schedule_broadcast()

    async def _handle_mktdata_message(self, message: str):
        """Handle market data updates from MKTDATA."""
        try:
            data = parse_json_msg(message)
        except json.JSONDecodeError:
            return

        msg_type = data.get("type")
        symbol = data.get("symbol", "")
        log_recv(logger, "MKTDATA", f"market_data {symbol} last={data.get('last', data.get('price'))}", message)

        if msg_type == "market_data":
            # Expect: {"type": "market_data", "symbol": "BTC/USD", "bid": ..., "ask": ..., "last": ...}
            symbol = data.get("symbol", "")
            # Use 'last' price, falling back to midpoint of bid/ask
            last = data.get("last") or data.get("price")
            if last is None:
                bid = data.get("bid")
                ask = data.get("ask")
                if bid is not None and ask is not None:
                    last = (float(bid) + float(ask)) / 2.0

            if symbol and last is not None:
                pos = self._get_position(symbol)
                new_price = float(last)
                if new_price != pos.market_price:
                    pos.market_price = new_price
                    await self._schedule_broadcast()

        elif msg_type == "price_update":
            # Alternative format: {"type": "price_update", "symbol": ..., "price": ...}
            symbol = data.get("symbol", "")
            price = data.get("price")
            if symbol and price is not None:
                pos = self._get_position(symbol)
                new_price = float(price)
                if new_price != pos.market_price:
                    pos.market_price = new_price
                    await self._schedule_broadcast()

    def _build_position_list(self) -> list:
        """Build a list of position dicts for broadcasting."""
        return [
            pos.to_dict() for pos in self.positions.values()
            if pos.qty != 0 or pos.realized_pnl != 0
        ]

    def _build_position_update(self) -> str:
        """Build a position_update JSON message with all positions."""
        return json.dumps({
            "type": "position_update",
            "positions": self._build_position_list(),
        })

    async def _schedule_broadcast(self):
        """Schedule a position broadcast, throttled to max 2 per second."""
        async with self._broadcast_lock:
            now = time.monotonic()
            elapsed = now - self._last_broadcast_time

            if elapsed >= self._broadcast_interval:
                # Enough time has passed, broadcast immediately
                await self._do_broadcast()
            elif not self._broadcast_pending:
                # Schedule a delayed broadcast
                self._broadcast_pending = True
                delay = self._broadcast_interval - elapsed

                def _schedule_delayed():
                    task = asyncio.ensure_future(self._delayed_broadcast())
                    task.add_done_callback(
                        lambda t: logger.error(f"Delayed broadcast error: {t.exception()}")
                        if t.exception() else None
                    )

                asyncio.get_running_loop().call_later(delay, _schedule_delayed)

    async def _delayed_broadcast(self):
        """Execute a delayed broadcast after throttle period."""
        async with self._broadcast_lock:
            self._broadcast_pending = False
            await self._do_broadcast()

    async def _do_broadcast(self):
        """Actually broadcast position update to all connected clients."""
        self._last_broadcast_time = time.monotonic()
        positions_list = self._build_position_list()
        if not positions_list:
            return
        msg = json.dumps({"type": "position_update", "positions": positions_list})
        await self.server.broadcast(msg)
        log_send(logger, "CLIENTS", f"position_update {len(positions_list)} positions", msg)

    async def start(self):
        """Start the Position Manager."""
        logger.info("Starting POSMANAGER...")

        # Start WS server
        await self.server.start()
        logger.info(f"POSMANAGER server listening on ws://{HOST}:{PORTS['POSMANAGER']}")

        # Connect to MKTDATA as client (in background with retry)
        self._mktdata_task = asyncio.create_task(self._connect_mktdata())
        self._mktdata_task.add_done_callback(self._task_exception_handler)

        logger.info("POSMANAGER started successfully")

    def _task_exception_handler(self, task: asyncio.Task):
        """Log exceptions from background tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"Background task failed: {exc}")

    async def _connect_mktdata(self):
        """Connect to MKTDATA and start listening. Retries on failure."""
        try:
            await self.mktdata_client.connect(retry=True)
            logger.info(f"MKTDATA connected. Fill sequence at reconnect: {self._fill_sequence}")
            await self.mktdata_client.listen()
        except Exception as e:
            logger.error(f"MKTDATA connection error: {e}")

    async def stop(self):
        """Stop the Position Manager."""
        logger.info("Stopping POSMANAGER...")
        await self.mktdata_client.close()
        await self.server.stop()
        logger.info("POSMANAGER stopped")


async def main():
    pm = PositionManager()
    await pm.start()

    # Keep running until interrupted
    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, _signal_handler)
        loop.add_signal_handler(signal.SIGTERM, _signal_handler)
    except NotImplementedError:
        # Windows doesn't support add_signal_handler
        pass

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        logger.warning("KeyboardInterrupt received during shutdown wait")
    finally:
        await pm.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("POSMANAGER terminated by user")
