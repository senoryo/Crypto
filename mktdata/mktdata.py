"""
Market Data (MKTDATA) Server - Main entry point.

Runs a WebSocket server on port 8081 that broadcasts simulated market data
from Binance and Coinbase feeds to all connected clients.

Clients can subscribe/unsubscribe to specific symbols. If no subscription
message is sent, the client receives data for ALL symbols.

Usage:
    python -m mktdata.mktdata
"""

import asyncio
import json
import logging
import signal
import sys
from typing import Dict, Optional, Set

from shared.config import HOST, PORTS, SYMBOLS, USE_REAL_COINBASE, USE_COINBASE_FIX
from shared.logging_config import setup_component_logging, log_recv, log_send
from shared.ws_transport import WSServer, json_msg, parse_json_msg

from mktdata.binance_feed import BinanceFeed

if USE_COINBASE_FIX:
    from mktdata.coinbase_fix_feed import CoinbaseFIXFeed as CoinbaseFeedClass
elif USE_REAL_COINBASE:
    from mktdata.coinbase_live_feed import CoinbaseLiveFeed as CoinbaseFeedClass
else:
    from mktdata.coinbase_feed import CoinbaseFeed as CoinbaseFeedClass

logger = setup_component_logging("MKTDATA")


class MarketDataServer:
    """Main MKTDATA server that manages feeds and client subscriptions."""

    def __init__(self):
        self._server = WSServer(HOST, PORTS["MKTDATA"], name="MKTDATA")
        self._binance_feed = BinanceFeed()
        self._coinbase_feed = CoinbaseFeedClass()

        # Track per-client subscriptions: websocket -> set of symbols (None = all)
        self._subscriptions: Dict[object, Optional[Set[str]]] = {}

        # Latest market data cache: (symbol, exchange) -> market_data_dict
        self._latest: Dict[tuple, dict] = {}

        # Tick counter for logging
        self._tick_count = 0

        # Register server handlers
        self._server.on_message(self._handle_message)
        self._server.on_connect(self._handle_connect)
        self._server.on_disconnect(self._handle_disconnect)

    async def _handle_connect(self, websocket):
        """Handle new client connection. Default: subscribe to all symbols."""
        self._subscriptions[websocket] = None  # None means all symbols
        logger.info(f"Client connected. Total clients: {len(self._subscriptions)}")

        # Send latest snapshots for all symbols from both exchanges
        for key, data in self._latest.items():
            try:
                snapshot = json.dumps(data)
                await websocket.send(snapshot)
                log_send(logger, "CLIENT", f"snapshot {key[0]}@{key[1]}", snapshot)
            except Exception as e:
                logger.warning(f"Failed to send snapshot {key[0]}@{key[1]} to client: {e}")

    async def _handle_disconnect(self, websocket):
        """Handle client disconnection."""
        self._subscriptions.pop(websocket, None)
        logger.info(f"Client disconnected. Total clients: {len(self._subscriptions)}")

    async def _handle_message(self, websocket, message: str):
        """Handle incoming messages from clients (subscribe/unsubscribe)."""
        try:
            msg = parse_json_msg(message)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON from client: {message[:100]}")
            return

        msg_type = msg.get("type", "")
        log_recv(logger, "CLIENT", f"{msg_type} {msg.get('symbols', '')}", message)

        if msg_type == "subscribe":
            symbols = msg.get("symbols", [])
            valid_symbols = [s for s in symbols if s in SYMBOLS]
            if not valid_symbols:
                logger.warning(f"Subscribe request with no valid symbols: {symbols}")
                return

            current = self._subscriptions.get(websocket)
            if current is None:
                # Was subscribed to all, now switch to explicit set
                self._subscriptions[websocket] = set(valid_symbols)
            else:
                current.update(valid_symbols)

            logger.info(f"Client subscribed to: {valid_symbols}")

            # Send latest snapshots for newly subscribed symbols
            for symbol in valid_symbols:
                for exchange in ("BINANCE", "COINBASE"):
                    key = (symbol, exchange)
                    if key in self._latest:
                        snapshot = json.dumps(self._latest[key])
                        try:
                            await websocket.send(snapshot)
                            log_send(logger, "CLIENT", f"snapshot {symbol}@{exchange}", snapshot)
                        except Exception as e:
                            logger.warning(f"Failed to send snapshot {symbol}@{exchange} to client: {e}")

        elif msg_type == "unsubscribe":
            symbols = msg.get("symbols", [])
            current = self._subscriptions.get(websocket)
            if current is None:
                # Was subscribed to all, switch to all minus unsubscribed
                self._subscriptions[websocket] = set(SYMBOLS) - set(symbols)
            elif isinstance(current, set):
                current -= set(symbols)

            logger.info(f"Client unsubscribed from: {symbols}")

        else:
            logger.debug(f"Unknown message type from client: {msg_type}")

    async def _on_market_data(self, data: dict):
        """Callback from feeds. Cache and broadcast to subscribed clients."""
        symbol = data["symbol"]
        exchange = data["exchange"]

        # Cache latest
        self._latest[(symbol, exchange)] = data

        log_recv(logger, "FEED", f"market_data {symbol}@{exchange} last={data.get('last')}", data)

        # Periodic logging
        self._tick_count += 1
        if self._tick_count % 50 == 0:
            logger.info(
                f"Tick #{self._tick_count}: {symbol}@{exchange} "
                f"last={data['last']} bid={data['bid']} ask={data['ask']}"
            )

        # Broadcast to subscribed clients
        message = json.dumps(data)
        for ws, subs in list(self._subscriptions.items()):
            if subs is None or symbol in subs:
                try:
                    await ws.send(message)
                    log_send(logger, "CLIENT", f"market_data {symbol}@{exchange}", message)
                except Exception as e:
                    # Client will be cleaned up by disconnect handler
                    logger.warning(f"Failed to send market_data {symbol}@{exchange} to client: {e}")

    async def start(self):
        """Start the MKTDATA server and all feeds."""
        logger.info("=" * 60)
        logger.info("MKTDATA Server starting...")
        logger.info(f"  Host: {HOST}")
        logger.info(f"  Port: {PORTS['MKTDATA']}")
        logger.info(f"  Symbols: {', '.join(SYMBOLS)}")
        logger.info(f"  Feeds: BINANCE, COINBASE")
        logger.info("=" * 60)

        # Start WebSocket server
        await self._server.start()

        # Start feeds
        await self._binance_feed.start(self._on_market_data)
        await self._coinbase_feed.start(self._on_market_data)

        logger.info("MKTDATA Server is running. Waiting for connections...")

    async def stop(self):
        """Stop the server and all feeds."""
        logger.info("MKTDATA Server shutting down...")
        await self._binance_feed.stop()
        await self._coinbase_feed.stop()
        await self._server.stop()
        logger.info("MKTDATA Server stopped.")

    def get_latest(self, symbol: str, exchange: str = None) -> Optional[dict]:
        """Get latest market data for a symbol, optionally from a specific exchange."""
        if exchange:
            return self._latest.get((symbol, exchange))
        # Return from any exchange
        for key, data in self._latest.items():
            if key[0] == symbol:
                return data
        return None


async def main():
    """Main entry point."""
    server = MarketDataServer()

    # Handle shutdown signals
    shutdown_event = asyncio.Event()

    def signal_handler():
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    # Register signal handlers (works on Unix; on Windows use try/except for KeyboardInterrupt)
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, signal_handler)
    except NotImplementedError:
        # Windows doesn't support add_signal_handler for SIGINT/SIGTERM
        pass

    await server.start()

    try:
        await shutdown_event.wait()
    except KeyboardInterrupt:
        logger.warning("KeyboardInterrupt received during shutdown wait")
    finally:
        await server.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("MKTDATA terminated by user")
