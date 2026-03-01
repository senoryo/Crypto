"""
Live Coinbase Advanced Trade market data feed.

Drop-in replacement for CoinbaseFeed (same interface: start(callback), stop()).
- Production: connects to Coinbase Advanced Trade WebSocket for public ticker data.
- Sandbox: falls back to the existing CoinbaseFeed simulator (sandbox has no WS).

Reconnects with exponential backoff (5s -> 60s max).
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

import websockets

from shared.config import (
    COINBASE_MODE,
    COINBASE_WS_MARKET_URL,
    EXCHANGES,
    SYMBOLS,
)

logger = logging.getLogger(__name__)

# Coinbase product IDs for our symbols
PRODUCT_IDS = list(EXCHANGES["COINBASE"]["symbols"].values())

# Reverse map: "BTC-USD" -> "BTC/USD"
_CB_TO_SYMBOL = {v: k for k, v in EXCHANGES["COINBASE"]["symbols"].items()}


class CoinbaseLiveFeed:
    """Live Coinbase market data feed via Advanced Trade WebSocket.

    Same interface as CoinbaseFeed: start(callback), stop().
    In sandbox mode, delegates to the simulator since sandbox has no WS feed.
    """

    def __init__(self):
        self.exchange = "COINBASE"
        self._running = False
        self._callback: Optional[Callable] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._simulator = None  # Lazy-loaded for sandbox fallback

    async def start(self, callback: Callable):
        """Start the market data feed.

        Args:
            callback: async function called with market_data dict for each tick
        """
        self._callback = callback
        self._running = True

        if COINBASE_MODE == "sandbox":
            logger.info(f"[{self.exchange}] Sandbox mode — using simulator for market data")
            from mktdata.coinbase_feed import CoinbaseFeed
            self._simulator = CoinbaseFeed()
            await self._simulator.start(callback)
        else:
            logger.info(f"[{self.exchange}] Production mode — connecting to live WebSocket")
            self._ws_task = asyncio.create_task(self._ws_loop())

    async def stop(self):
        """Stop the feed."""
        self._running = False
        if self._simulator:
            await self._simulator.stop()
            self._simulator = None
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None
        logger.info(f"[{self.exchange}] Live feed stopped")

    async def _ws_loop(self):
        """Main WebSocket loop with reconnection and exponential backoff."""
        backoff = 5.0
        max_backoff = 60.0

        while self._running:
            try:
                async with websockets.connect(COINBASE_WS_MARKET_URL) as ws:
                    logger.info(f"[{self.exchange}] WebSocket connected to {COINBASE_WS_MARKET_URL}")
                    backoff = 5.0  # Reset on successful connect

                    # Subscribe to public ticker channel (no auth needed)
                    subscribe_msg = {
                        "type": "subscribe",
                        "product_ids": PRODUCT_IDS,
                        "channel": "ticker",
                    }
                    await ws.send(json.dumps(subscribe_msg))
                    logger.info(f"[{self.exchange}] Subscribed to ticker for {PRODUCT_IDS}")

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            await self._handle_message(msg)
                        except json.JSONDecodeError:
                            logger.warning(f"[{self.exchange}] Invalid JSON from WS: {raw[:200]}")
                        except Exception as e:
                            logger.error(f"[{self.exchange}] Error handling WS message: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                logger.warning(
                    f"[{self.exchange}] WebSocket disconnected: {e}. "
                    f"Reconnecting in {backoff:.0f}s..."
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    async def _handle_message(self, msg: dict):
        """Parse a Coinbase WS message and call the callback if it's a ticker event."""
        channel = msg.get("channel")
        if channel != "ticker":
            return

        events = msg.get("events", [])
        for event in events:
            tickers = event.get("tickers", [])
            for ticker in tickers:
                product_id = ticker.get("product_id", "")
                symbol = _CB_TO_SYMBOL.get(product_id)
                if not symbol:
                    continue

                try:
                    price = float(ticker.get("price", 0))
                    # Coinbase ticker provides best_bid, best_ask, etc.
                    best_bid = float(ticker.get("best_bid", price))
                    best_ask = float(ticker.get("best_ask", price))
                    volume_24h = float(ticker.get("volume_24_h", 0))
                    price_pct_chg_24h = float(ticker.get("price_percent_chg_24_h", 0))
                    best_bid_qty = float(ticker.get("best_bid_quantity", 0))
                    best_ask_qty = float(ticker.get("best_ask_quantity", 0))
                except (ValueError, TypeError):
                    continue

                market_data = {
                    "type": "market_data",
                    "symbol": symbol,
                    "bid": best_bid,
                    "ask": best_ask,
                    "last": price,
                    "bid_size": best_bid_qty,
                    "ask_size": best_ask_qty,
                    "volume": round(volume_24h, 2),
                    "change_pct": round(price_pct_chg_24h, 2),
                    "exchange": self.exchange,
                    "timestamp": datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%S.%f"
                    )[:-3] + "Z",
                }

                if self._callback:
                    await self._callback(market_data)
