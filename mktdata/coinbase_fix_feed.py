"""
Coinbase FIX Market Data Feed.

Drop-in replacement for CoinbaseFeed / CoinbaseLiveFeed
(same interface: start(callback), stop()).

Connects to Coinbase Exchange FIX 5.0 SP2 market data endpoint via TCP+SSL.
Subscribes to top-of-book data for all configured symbols.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from shared.config import (
    CB_FIX_API_KEY,
    CB_FIX_PASSPHRASE,
    CB_FIX_SECRET,
    COINBASE_FIX_MD_HOST,
    COINBASE_FIX_PORT,
    COINBASE_MODE,
    EXCHANGES,
    SYMBOLS,
)
from shared.fix_engine import FIXClient, FIXSession, FIXWireMessage

logger = logging.getLogger(__name__)

# Coinbase product IDs for our symbols
_SYMBOL_TO_PRODUCT = EXCHANGES["COINBASE"]["symbols"]
_PRODUCT_TO_SYMBOL = {v: k for k, v in _SYMBOL_TO_PRODUCT.items()}


class CoinbaseFIXFeed:
    """Coinbase FIX 5.0 SP2 market data feed.

    Same interface as CoinbaseFeed: start(callback), stop().
    """

    def __init__(self):
        self.exchange = "COINBASE"
        self._running = False
        self._callback: Optional[Callable] = None
        self._run_task: Optional[asyncio.Task] = None
        self._md_req_id_counter = 0

        # Latest prices per symbol for building complete market data dicts
        self._latest = {}  # symbol -> {bid, ask, last, bid_size, ask_size}

        # FIX client
        host = COINBASE_FIX_MD_HOST.get(COINBASE_MODE, COINBASE_FIX_MD_HOST["sandbox"])
        session = FIXSession(
            sender_comp_id=CB_FIX_API_KEY,
            target_comp_id="Coinbase",
            heartbeat_interval=30,
        )
        self._client = FIXClient(
            host=host,
            port=COINBASE_FIX_PORT,
            session=session,
            password=CB_FIX_PASSPHRASE,
            api_secret_b64=CB_FIX_SECRET,
            on_message=self._on_fix_message,
            name="FIX-MD",
        )
        self._client.on_logon = self._on_connected

    async def start(self, callback: Callable):
        self._callback = callback
        self._running = True

        if not CB_FIX_API_KEY or not CB_FIX_SECRET:
            logger.warning(
                f"[{self.exchange}] No FIX credentials configured. "
                "Set CB_FIX_API_KEY, CB_FIX_PASSPHRASE, CB_FIX_SECRET in .env"
            )
            # Fall back to simulator
            logger.info(f"[{self.exchange}] Falling back to simulated feed")
            from mktdata.coinbase_feed import CoinbaseFeed
            self._fallback_feed = CoinbaseFeed()
            await self._fallback_feed.start(callback)
            return

        self._run_task = asyncio.create_task(self._client.run(auto_reconnect=True))
        logger.info(f"[{self.exchange}] FIX market data feed started (mode={COINBASE_MODE})")

    async def stop(self):
        self._running = False

        if hasattr(self, "_fallback_feed") and self._fallback_feed:
            await self._fallback_feed.stop()
            self._fallback_feed = None
            return

        await self._client.stop()
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
            self._run_task = None
        logger.info(f"[{self.exchange}] FIX market data feed stopped")

    async def _on_connected(self):
        """Called after FIX Logon — subscribe to market data for all symbols."""
        logger.info(f"[{self.exchange}] FIX Logon confirmed, subscribing to market data")

        for symbol in SYMBOLS:
            product_id = _SYMBOL_TO_PRODUCT.get(symbol)
            if not product_id:
                continue

            self._md_req_id_counter += 1
            md_req_id = f"MDSUB-{self._md_req_id_counter}"

            wire = FIXWireMessage()
            wire.set(35, "V")          # MsgType = MarketDataRequest
            wire.set(262, md_req_id)   # MDReqID
            wire.set(263, 1)           # SubscriptionRequestType = Snapshot + Updates
            wire.set(264, 1)           # MarketDepth = Top of Book
            wire.set(265, 1)           # MDUpdateType = Incremental Refresh

            # MDEntryTypes repeating group
            wire.set(267, 3)           # NoMDEntryTypes = 3
            # Bid, Offer, Trade
            # Use repeating tag 269 for each entry type
            wire._fields.append((269, "0"))  # MDEntryType = Bid
            wire._fields.append((269, "1"))  # MDEntryType = Offer
            wire._fields.append((269, "2"))  # MDEntryType = Trade

            # Instruments repeating group
            wire.set(146, 1)           # NoRelatedSym = 1
            wire._fields.append((55, product_id))  # Symbol

            try:
                await self._client.send(wire)
                logger.info(
                    f"[{self.exchange}] MarketDataRequest sent for {product_id} "
                    f"(req_id={md_req_id})"
                )
            except Exception as e:
                logger.error(
                    f"[{self.exchange}] Failed to subscribe {product_id}: {e}"
                )

    async def _on_fix_message(self, wire_msg: FIXWireMessage):
        """Handle incoming FIX market data messages."""
        msg_type = wire_msg.msg_type

        if msg_type == "W":  # MarketDataSnapshotFullRefresh
            await self._handle_snapshot(wire_msg)
        elif msg_type == "X":  # MarketDataIncrementalRefresh
            await self._handle_incremental(wire_msg)
        elif msg_type == "Y":  # MarketDataRequestReject
            reason = wire_msg.get(58)
            md_req_id = wire_msg.get(262)
            logger.warning(
                f"[{self.exchange}] MarketDataRequestReject: "
                f"req_id={md_req_id} reason={reason}"
            )
        else:
            logger.debug(f"[{self.exchange}] Unhandled FIX msg type: {msg_type}")

    async def _handle_snapshot(self, wire: FIXWireMessage):
        """Parse MarketDataSnapshotFullRefresh (35=W).

        Extract bid/ask/trade from the repeating MDEntry group.
        """
        wire_symbol = wire.get(55)
        symbol = _PRODUCT_TO_SYMBOL.get(wire_symbol, wire_symbol)

        if symbol not in self._latest:
            self._latest[symbol] = {
                "bid": 0.0, "ask": 0.0, "last": 0.0,
                "bid_size": 0.0, "ask_size": 0.0,
            }

        data = self._latest[symbol]

        # Parse repeating group: each MDEntry has 269 (type), 270 (price), 271 (size)
        entries = self._parse_md_entries(wire)
        for entry in entries:
            entry_type = entry.get("type", "")
            price = entry.get("price", 0.0)
            size = entry.get("size", 0.0)

            if entry_type == "0":      # Bid
                data["bid"] = price
                data["bid_size"] = size
            elif entry_type == "1":    # Offer
                data["ask"] = price
                data["ask_size"] = size
            elif entry_type == "2":    # Trade
                data["last"] = price

        await self._publish(symbol, data)

    async def _handle_incremental(self, wire: FIXWireMessage):
        """Parse MarketDataIncrementalRefresh (35=X).

        Handle MDUpdateAction(279): 0=New, 1=Change, 2=Delete
        """
        entries = self._parse_md_entries(wire)

        for entry in entries:
            wire_symbol = entry.get("symbol", wire.get(55))
            symbol = _PRODUCT_TO_SYMBOL.get(wire_symbol, wire_symbol)

            if symbol not in self._latest:
                self._latest[symbol] = {
                    "bid": 0.0, "ask": 0.0, "last": 0.0,
                    "bid_size": 0.0, "ask_size": 0.0,
                }

            data = self._latest[symbol]
            entry_type = entry.get("type", "")
            action = entry.get("action", "0")  # 0=New, 1=Change
            price = entry.get("price", 0.0)
            size = entry.get("size", 0.0)

            if action == "2":  # Delete
                if entry_type == "0":
                    data["bid"] = 0.0
                    data["bid_size"] = 0.0
                elif entry_type == "1":
                    data["ask"] = 0.0
                    data["ask_size"] = 0.0
                continue

            if entry_type == "0":      # Bid
                data["bid"] = price
                data["bid_size"] = size
            elif entry_type == "1":    # Offer
                data["ask"] = price
                data["ask_size"] = size
            elif entry_type == "2":    # Trade
                data["last"] = price

            await self._publish(symbol, data)

    def _parse_md_entries(self, wire: FIXWireMessage) -> list:
        """Parse repeating group of MD entries from a FIX wire message.

        Walks through fields looking for MDEntryType(269) to delimit entries,
        then collects price(270), size(271), symbol(55), action(279).
        """
        entries = []
        current_entry = None

        for tag, value in wire._fields:
            if tag == 269:  # MDEntryType — start of new entry
                if current_entry is not None:
                    entries.append(current_entry)
                current_entry = {"type": value}
            elif current_entry is not None:
                if tag == 270:    # MDEntryPx
                    try:
                        current_entry["price"] = float(value)
                    except ValueError as e:
                        logger.warning(f"Invalid MDEntryPx value '{value}': {e}")
                elif tag == 271:  # MDEntrySize
                    try:
                        current_entry["size"] = float(value)
                    except ValueError as e:
                        logger.warning(f"Invalid MDEntrySize value '{value}': {e}")
                elif tag == 55:   # Symbol
                    current_entry["symbol"] = value
                elif tag == 279:  # MDUpdateAction
                    current_entry["action"] = value

        if current_entry is not None:
            entries.append(current_entry)

        return entries

    async def _publish(self, symbol: str, data: dict):
        """Build and publish a standard market_data dict."""
        if not self._callback:
            return

        # Only publish if we have meaningful data
        if data["bid"] <= 0 and data["ask"] <= 0 and data["last"] <= 0:
            return

        market_data = {
            "type": "market_data",
            "symbol": symbol,
            "bid": data["bid"],
            "ask": data["ask"],
            "last": data["last"],
            "bid_size": data["bid_size"],
            "ask_size": data["ask_size"],
            "volume": 0,       # Not available from FIX TOB
            "change_pct": 0,   # Not available from FIX TOB
            "exchange": self.exchange,
            "timestamp": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%f"
            )[:-3] + "Z",
        }

        await self._callback(market_data)
