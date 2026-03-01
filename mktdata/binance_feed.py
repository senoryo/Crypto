"""Simulated Binance market data feed."""

import asyncio
import random
import logging
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

from shared.config import SYMBOLS, EXCHANGES

logger = logging.getLogger(__name__)

# Base prices for each symbol
BASE_PRICES: Dict[str, float] = {
    "BTC/USD": 67500.0,
    "ETH/USD": 3450.0,
    "SOL/USD": 178.0,
    "ADA/USD": 0.72,
    "DOGE/USD": 0.165,
}

# Price precision (decimal places) per symbol
PRICE_PRECISION: Dict[str, int] = {
    "BTC/USD": 2,
    "ETH/USD": 2,
    "SOL/USD": 3,
    "ADA/USD": 5,
    "DOGE/USD": 6,
}


class BinanceFeed:
    """Simulated Binance market data feed.

    Generates realistic price ticks using a random walk model with
    occasional larger moves to simulate real market activity.
    """

    def __init__(self):
        self.exchange = "BINANCE"
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._prices: Dict[str, float] = {}
        self._volumes: Dict[str, float] = {}
        self._open_prices: Dict[str, float] = {}
        self._callback: Optional[Callable] = None

        # Initialize prices with a slight Binance-specific offset
        for symbol, base in BASE_PRICES.items():
            offset = base * random.uniform(-0.0001, 0.0001)
            self._prices[symbol] = base + offset
            self._open_prices[symbol] = self._prices[symbol]
            self._volumes[symbol] = random.uniform(1000, 5000)

    async def start(self, callback: Callable):
        """Start generating market data ticks.

        Args:
            callback: async function called with market_data_dict for each tick
        """
        self._callback = callback
        self._running = True
        logger.info(f"[{self.exchange}] Feed starting for {len(SYMBOLS)} symbols")

        for symbol in SYMBOLS:
            task = asyncio.create_task(self._generate_ticks(symbol))
            self._tasks.append(task)

        logger.info(f"[{self.exchange}] Feed started")

    async def stop(self):
        """Stop the feed."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info(f"[{self.exchange}] Feed stopped")

    async def _generate_ticks(self, symbol: str):
        """Generate price ticks for a single symbol using random walk."""
        precision = PRICE_PRECISION.get(symbol, 4)

        while self._running:
            try:
                interval = random.uniform(0.5, 1.5)
                await asyncio.sleep(interval)

                if not self._running:
                    break

                current_price = self._prices[symbol]

                # Determine move magnitude: occasionally generate larger moves
                if random.random() < 0.05:
                    # Large move (5% chance): 0.5-1.0% of price
                    pct_change = random.uniform(0.005, 0.01)
                else:
                    # Normal move: 0.01-0.1% of price
                    pct_change = random.uniform(0.0001, 0.001)

                # Random direction
                direction = random.choice([-1, 1])
                move = current_price * pct_change * direction

                # Apply mean reversion toward base price (prevents drift too far)
                base = BASE_PRICES[symbol]
                reversion = (base - current_price) * 0.001
                move += reversion

                new_price = round(current_price + move, precision)
                # Ensure price stays positive
                new_price = max(new_price, round(base * 0.5, precision))

                self._prices[symbol] = new_price

                # Calculate spread (0.01% of price)
                spread = new_price * 0.0001
                bid = round(new_price - spread, precision)
                ask = round(new_price + spread, precision)

                # Volume per tick
                tick_volume = round(random.uniform(0.1, 10.0), 4)
                self._volumes[symbol] += tick_volume

                # Daily change percentage from open
                open_price = self._open_prices[symbol]
                change_pct = round(((new_price - open_price) / open_price) * 100, 2)

                # Build market data message
                market_data = {
                    "type": "market_data",
                    "symbol": symbol,
                    "bid": bid,
                    "ask": ask,
                    "last": new_price,
                    "bid_size": round(random.uniform(0.5, 15.0), 4),
                    "ask_size": round(random.uniform(0.5, 15.0), 4),
                    "volume": round(self._volumes[symbol], 2),
                    "change_pct": change_pct,
                    "exchange": self.exchange,
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                }

                if self._callback:
                    await self._callback(market_data)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{self.exchange}] Error generating tick for {symbol}: {e}")
                await asyncio.sleep(1.0)

    def get_price(self, symbol: str) -> Optional[float]:
        """Get the current price for a symbol."""
        return self._prices.get(symbol)

    def get_snapshot(self, symbol: str) -> Optional[dict]:
        """Get a full market data snapshot for a symbol."""
        if symbol not in self._prices:
            return None

        price = self._prices[symbol]
        precision = PRICE_PRECISION.get(symbol, 4)
        spread = price * 0.0001
        open_price = self._open_prices[symbol]
        change_pct = round(((price - open_price) / open_price) * 100, 2)

        return {
            "type": "market_data",
            "symbol": symbol,
            "bid": round(price - spread, precision),
            "ask": round(price + spread, precision),
            "last": price,
            "bid_size": round(random.uniform(0.5, 15.0), 4),
            "ask_size": round(random.uniform(0.5, 15.0), 4),
            "volume": round(self._volumes.get(symbol, 0), 2),
            "change_pct": change_pct,
            "exchange": self.exchange,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        }
