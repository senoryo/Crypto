"""
Coinbase FIX Order Entry Adapter.

Drop-in replacement for CoinbaseAdapter / CoinbaseSimulator
(same interface: set_report_callback, start, stop, submit_order,
cancel_order, amend_order).

Connects to Coinbase Exchange FIX 5.0 SP2 order entry endpoint via TCP+SSL.
"""

import asyncio
import logging
import time
import uuid
from typing import Callable, Dict, Optional

from shared.config import (
    CB_FIX_API_KEY,
    CB_FIX_PASSPHRASE,
    CB_FIX_SECRET,
    COINBASE_FIX_ORD_HOST,
    COINBASE_FIX_PORT,
    COINBASE_MODE,
    EXCHANGES,
)
from shared.fix_engine import FIXClient, FIXSession, FIXWireMessage
from shared.fix_protocol import (
    FIXMessage,
    Tag,
    ExecType,
    OrdStatus,
    OrdType,
    execution_report,
)

logger = logging.getLogger(__name__)

# Map our symbols to Coinbase product IDs: "BTC/USD" -> "BTC-USD"
_SYMBOL_TO_PRODUCT = EXCHANGES["COINBASE"]["symbols"]
_PRODUCT_TO_SYMBOL = {v: k for k, v in _SYMBOL_TO_PRODUCT.items()}


class _TrackedOrder:
    """Internal state for an order submitted over FIX."""

    def __init__(self, cl_ord_id: str, symbol: str, side: str,
                 qty: float, ord_type: str, price: float = 0.0):
        self.cl_ord_id = cl_ord_id
        self.symbol = symbol
        self.side = side
        self.total_qty = qty
        self.ord_type = ord_type
        self.price = price
        self.order_id = ""
        self.cum_qty = 0.0
        self.avg_px = 0.0
        self.leaves_qty = qty
        self.is_terminal = False
        self._amend_fallback_orig: Optional[str] = None
        self._amend_fallback_msg = None


class CoinbaseFIXAdapter:
    """Coinbase FIX 5.0 SP2 order entry adapter."""

    def __init__(self):
        self.name = "COINBASE"
        self._report_callback: Optional[Callable] = None
        self._running = False
        self._run_task: Optional[asyncio.Task] = None

        # FIX client
        host = COINBASE_FIX_ORD_HOST.get(COINBASE_MODE, COINBASE_FIX_ORD_HOST["sandbox"])
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
            name="FIX-ORD",
        )

        # Order tracking
        self._orders: Dict[str, _TrackedOrder] = {}     # cl_ord_id -> _TrackedOrder
        # _pending_cancels removed — was populated but never consumed

    def set_report_callback(self, callback: Callable):
        self._report_callback = callback

    async def start(self):
        self._running = True
        if not CB_FIX_API_KEY or not CB_FIX_SECRET:
            logger.warning(
                f"[{self.name}] No FIX credentials configured. "
                "Set CB_FIX_API_KEY, CB_FIX_PASSPHRASE, CB_FIX_SECRET in .env"
            )
            return
        self._run_task = asyncio.create_task(self._client.run(auto_reconnect=True))
        logger.info(f"[{self.name}] FIX order entry adapter started (mode={COINBASE_MODE})")

    async def stop(self):
        self._running = False
        await self._client.stop()
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
            self._run_task = None
        logger.info(f"[{self.name}] FIX order entry adapter stopped")

    # ---- Order submission ----

    async def submit_order(self, fix_msg: FIXMessage):
        """Translate internal FIXMessage to wire NewOrderSingle (35=D) and send."""
        cl_ord_id = fix_msg.get(Tag.ClOrdID)
        symbol = fix_msg.get(Tag.Symbol)
        side = fix_msg.get(Tag.Side)
        qty = float(fix_msg.get(Tag.OrderQty, "0"))
        ord_type = fix_msg.get(Tag.OrdType)
        price = float(fix_msg.get(Tag.Price, "0"))

        if not self._client._connected:
            await self._send_reject(cl_ord_id, symbol, side, "FIX not connected")
            return

        product_id = _SYMBOL_TO_PRODUCT.get(symbol)
        if not product_id:
            await self._send_reject(cl_ord_id, symbol, side, f"Unknown symbol: {symbol}")
            return

        # Track order
        tracked = _TrackedOrder(cl_ord_id, symbol, side, qty, ord_type, price)
        self._orders[cl_ord_id] = tracked

        # Build wire NewOrderSingle
        wire = FIXWireMessage()
        wire.set(35, "D")          # MsgType = NewOrderSingle
        wire.set(11, cl_ord_id)    # ClOrdID
        wire.set(55, product_id)   # Symbol (Coinbase product ID)
        wire.set(54, side)         # Side (1=Buy, 2=Sell)
        wire.set(38, str(qty))     # OrderQty
        wire.set(44, str(price))   # Price
        wire.set(60, _fix_utc())   # TransactTime

        # TimeInForce and OrdType
        if ord_type == OrdType.Market:
            wire.set(40, "1")      # OrdType = Market
            wire.set(59, "3")      # TimeInForce = IOC for market
        elif ord_type == OrdType.Limit:
            wire.set(40, "2")      # OrdType = Limit
            wire.set(59, "1")      # TimeInForce = GTC (Coinbase uses 1 for GTC)
        else:
            wire.set(40, ord_type)
            wire.set(59, "1")

        # SelfTradePrevention
        wire.set(7928, "D")        # DecrementAndCancel

        try:
            await self._client.send(wire)
            logger.info(
                f"[{self.name}] NewOrderSingle sent: cl={cl_ord_id} "
                f"{product_id} {'BUY' if side == '1' else 'SELL'} {qty} "
                f"{'MKT' if ord_type == OrdType.Market else f'LMT@{price}'}"
            )
        except Exception as e:
            logger.error(f"[{self.name}] Failed to send order: {e}")
            await self._send_reject(cl_ord_id, symbol, side, str(e))

    # ---- Cancel ----

    async def cancel_order(self, fix_msg: FIXMessage):
        """Send OrderCancelRequest (35=F)."""
        orig_cl_ord_id = fix_msg.get(Tag.OrigClOrdID)
        cl_ord_id = fix_msg.get(Tag.ClOrdID)
        symbol = fix_msg.get(Tag.Symbol)
        side = fix_msg.get(Tag.Side)

        if not self._client._connected:
            await self._send_reject(cl_ord_id, symbol, side, "FIX not connected")
            return

        tracked = self._orders.get(orig_cl_ord_id)
        if not tracked:
            await self._send_reject(cl_ord_id, symbol, side,
                                    f"Unknown order: {orig_cl_ord_id}")
            return

        product_id = _SYMBOL_TO_PRODUCT.get(symbol, symbol)

        wire = FIXWireMessage()
        wire.set(35, "F")              # MsgType = OrderCancelRequest
        wire.set(11, cl_ord_id)        # ClOrdID
        wire.set(41, orig_cl_ord_id)   # OrigClOrdID
        wire.set(55, product_id)       # Symbol
        wire.set(54, side)             # Side
        wire.set(60, _fix_utc())       # TransactTime

        # If we have an exchange-assigned OrderID, include it
        if tracked.order_id:
            wire.set(37, tracked.order_id)

        try:
            await self._client.send(wire)
            logger.info(f"[{self.name}] CancelRequest sent: cl={cl_ord_id} orig={orig_cl_ord_id}")
        except Exception as e:
            logger.error(f"[{self.name}] Failed to send cancel: {e}")
            await self._send_reject(cl_ord_id, symbol, side, str(e))

    # ---- Amend ----

    async def amend_order(self, fix_msg: FIXMessage):
        """Send OrderCancelReplaceRequest (35=G). Falls back to cancel+new if rejected."""
        orig_cl_ord_id = fix_msg.get(Tag.OrigClOrdID)
        cl_ord_id = fix_msg.get(Tag.ClOrdID)
        symbol = fix_msg.get(Tag.Symbol)
        side = fix_msg.get(Tag.Side)
        new_qty = float(fix_msg.get(Tag.OrderQty, "0"))
        new_price = float(fix_msg.get(Tag.Price, "0"))

        if not self._client._connected:
            await self._send_reject(cl_ord_id, symbol, side, "FIX not connected")
            return

        tracked = self._orders.get(orig_cl_ord_id)
        if not tracked:
            await self._send_reject(cl_ord_id, symbol, side,
                                    f"Unknown order: {orig_cl_ord_id}")
            return

        if tracked.ord_type == OrdType.Market:
            await self._send_reject(cl_ord_id, tracked.symbol, tracked.side,
                                    "Market orders cannot be amended")
            return

        product_id = _SYMBOL_TO_PRODUCT.get(symbol, symbol)

        wire = FIXWireMessage()
        wire.set(35, "G")              # MsgType = OrderCancelReplaceRequest
        wire.set(11, cl_ord_id)        # ClOrdID
        wire.set(41, orig_cl_ord_id)   # OrigClOrdID
        wire.set(55, product_id)       # Symbol
        wire.set(54, side)             # Side
        wire.set(40, "2")              # OrdType = Limit
        wire.set(38, str(new_qty))     # OrderQty
        wire.set(44, str(new_price))   # Price
        wire.set(59, "1")              # TimeInForce = GTC
        wire.set(60, _fix_utc())       # TransactTime

        if tracked.order_id:
            wire.set(37, tracked.order_id)

        # Store amend info for fallback
        self._orders[cl_ord_id] = _TrackedOrder(
            cl_ord_id, symbol, side, new_qty, tracked.ord_type, new_price,
        )
        self._orders[cl_ord_id]._amend_fallback_orig = orig_cl_ord_id
        self._orders[cl_ord_id]._amend_fallback_msg = fix_msg

        try:
            await self._client.send(wire)
            logger.info(
                f"[{self.name}] CancelReplaceRequest sent: cl={cl_ord_id} "
                f"orig={orig_cl_ord_id} qty={new_qty} price={new_price}"
            )
        except Exception as e:
            logger.error(f"[{self.name}] Failed to send amend: {e}")
            await self._send_reject(cl_ord_id, symbol, side, str(e))

    # ---- Incoming FIX message handling ----

    async def _on_fix_message(self, wire_msg: FIXWireMessage):
        """Handle incoming ExecutionReport (35=8) and OrderCancelReject (35=9)."""
        msg_type = wire_msg.msg_type

        if msg_type == "8":  # ExecutionReport
            await self._handle_execution_report(wire_msg)
        elif msg_type == "9":  # OrderCancelReject
            await self._handle_cancel_reject(wire_msg)
        else:
            logger.debug(f"[{self.name}] Unhandled FIX msg type: {msg_type}")

    async def _handle_execution_report(self, wire: FIXWireMessage):
        """Translate wire ExecutionReport to internal FIXMessage and forward."""
        cl_ord_id = wire.get(11)
        order_id = wire.get(37)
        exec_type = wire.get(150)
        ord_status = wire.get(39)
        wire_symbol = wire.get(55)
        side = wire.get(54)
        leaves_qty = wire.get_float(151)
        cum_qty = wire.get_float(14)
        avg_px = wire.get_float(6)
        last_px = wire.get_float(31)
        last_qty = wire.get_float(32)
        text = wire.get(58)

        # Map Coinbase product ID back to our symbol
        symbol = _PRODUCT_TO_SYMBOL.get(wire_symbol, wire_symbol)

        # Update tracked order
        tracked = self._orders.get(cl_ord_id)
        if tracked:
            tracked.order_id = order_id
            tracked.cum_qty = cum_qty
            tracked.avg_px = avg_px
            tracked.leaves_qty = leaves_qty
            if exec_type in ("4", "C", "8"):  # Canceled, Expired, Rejected
                tracked.is_terminal = True

        logger.info(
            f"[{self.name}] ExecReport: cl={cl_ord_id} oid={order_id} "
            f"exec={exec_type} status={ord_status} {symbol} "
            f"leaves={leaves_qty} cum={cum_qty} avg={avg_px}"
            + (f" text={text}" if text else "")
        )

        # Build internal execution report
        report = execution_report(
            cl_ord_id=cl_ord_id,
            order_id=order_id or "NONE",
            exec_type=exec_type,
            ord_status=ord_status,
            symbol=symbol,
            side=side,
            leaves_qty=leaves_qty,
            cum_qty=cum_qty,
            avg_px=avg_px,
            last_px=last_px,
            last_qty=last_qty,
            text=text,
        )
        await self._send_report(report)

    async def _handle_cancel_reject(self, wire: FIXWireMessage):
        """Handle OrderCancelReject (35=9)."""
        cl_ord_id = wire.get(11)
        orig_cl_ord_id = wire.get(41)
        text = wire.get(58)
        cxl_rej_reason = wire.get(102)

        logger.warning(
            f"[{self.name}] CancelReject: cl={cl_ord_id} orig={orig_cl_ord_id} "
            f"reason={cxl_rej_reason} text={text}"
        )

        # Check if this was an amend rejection — fall back to cancel+new
        tracked = self._orders.get(cl_ord_id)
        if tracked and hasattr(tracked, "_amend_fallback_orig"):
            logger.info(f"[{self.name}] Amend rejected, falling back to cancel+new")
            # Cancel the original
            orig_tracked = self._orders.get(tracked._amend_fallback_orig)
            if orig_tracked and not orig_tracked.is_terminal:
                cancel_id = f"CXL-{uuid.uuid4().hex[:8]}"
                from shared.fix_protocol import cancel_request
                cancel_msg = cancel_request(
                    cl_ord_id=cancel_id,
                    orig_cl_ord_id=tracked._amend_fallback_orig,
                    symbol=orig_tracked.symbol,
                    side=orig_tracked.side,
                )
                await self.cancel_order(cancel_msg)
                # Queue new order after small delay
                await asyncio.sleep(0.5)
                from shared.fix_protocol import new_order_single
                new_msg = new_order_single(
                    cl_ord_id=cl_ord_id,
                    symbol=tracked.symbol,
                    side=tracked.side,
                    qty=tracked.total_qty,
                    ord_type=tracked.ord_type,
                    price=tracked.price,
                    exchange="COINBASE",
                )
                await self.submit_order(new_msg)
            return

        # Regular cancel reject — find original order to get symbol/side
        orig_tracked = self._orders.get(orig_cl_ord_id)
        symbol = orig_tracked.symbol if orig_tracked else ""
        side = orig_tracked.side if orig_tracked else ""

        report = execution_report(
            cl_ord_id=cl_ord_id,
            order_id=wire.get(37, "NONE"),
            exec_type=ExecType.Rejected,
            ord_status=OrdStatus.Rejected,
            symbol=symbol,
            side=side,
            leaves_qty=0.0,
            cum_qty=0.0,
            avg_px=0.0,
            text=text or f"Cancel rejected: {cxl_rej_reason}",
        )
        await self._send_report(report)

    # ---- Helpers ----

    async def _send_report(self, report: FIXMessage):
        if self._report_callback:
            report.set(Tag.ExDestination, self.name)
            try:
                await self._report_callback(report)
            except Exception as e:
                logger.error(f"[{self.name}] Failed to send report: {e}")

    async def _send_reject(self, cl_ord_id: str, symbol: str, side: str, reason: str):
        logger.warning(f"[{self.name}] Reject: cl={cl_ord_id} — {reason}")
        report = execution_report(
            cl_ord_id=cl_ord_id,
            order_id="NONE",
            exec_type=ExecType.Rejected,
            ord_status=OrdStatus.Rejected,
            symbol=symbol,
            side=side,
            leaves_qty=0.0,
            cum_qty=0.0,
            avg_px=0.0,
            text=reason,
        )
        await self._send_report(report)


def _fix_utc() -> str:
    """UTC timestamp in FIX format."""
    t = time.time()
    gm = time.gmtime(t)
    ms = int((t % 1) * 1000)
    return time.strftime("%Y%m%d-%H:%M:%S", gm) + f".{ms:03d}"
