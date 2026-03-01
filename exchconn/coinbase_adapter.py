"""
Real Coinbase Advanced Trade exchange adapter.

Drop-in replacement for CoinbaseSimulator (same interface:
set_report_callback, start, stop, submit_order, cancel_order, amend_order).

Uses the coinbase-advanced-py SDK for REST and authenticated WebSocket
for fill updates (production) or REST polling (sandbox).
"""

import asyncio
import logging
from typing import Callable, Dict, Optional

# Monkey-patch the SDK's JWT builder to support Ed25519 CDP keys
import coinbase.jwt_generator as _cb_jwt
from shared.coinbase_auth import _load_key as _cb_load_key
import secrets as _secrets, time as _time, jwt as _jwt

def _patched_build_jwt(key_var, secret_var, uri=None):
    private_key, algorithm = _cb_load_key(secret_var)
    jwt_data = {
        "sub": key_var,
        "iss": "cdp",
        "nbf": int(_time.time()),
        "exp": int(_time.time()) + 120,
    }
    if uri:
        jwt_data["uri"] = uri
    return _jwt.encode(
        jwt_data, private_key, algorithm=algorithm,
        headers={"kid": key_var, "nonce": _secrets.token_hex()},
    )

_cb_jwt.build_jwt = _patched_build_jwt

from coinbase.rest import RESTClient

from shared.config import (
    COINBASE_API_KEY_NAME,
    COINBASE_API_SECRET,
    COINBASE_MODE,
    COINBASE_WS_USER_URL,
    EXCHANGES,
)
from shared.coinbase_auth import build_ws_subscribe_message
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

# Coinbase order statuses that are terminal (no more updates expected)
_TERMINAL_STATUSES = {"FILLED", "CANCELLED", "EXPIRED", "FAILED"}


class _TrackedOrder:
    """Internal state for an order submitted to Coinbase."""

    def __init__(self, cl_ord_id: str, cb_order_id: str, symbol: str, side: str,
                 qty: float, ord_type: str, price: float = 0.0):
        self.cl_ord_id = cl_ord_id
        self.cb_order_id = cb_order_id
        self.symbol = symbol
        self.side = side
        self.total_qty = qty
        self.ord_type = ord_type
        self.price = price
        self.cum_qty = 0.0
        self.avg_px = 0.0
        self.leaves_qty = qty
        self.is_terminal = False


class CoinbaseAdapter:
    """Real Coinbase exchange adapter using Advanced Trade API."""

    def __init__(self):
        self.name = "COINBASE"
        self._report_callback: Optional[Callable] = None
        self._client: Optional[RESTClient] = None
        self._running = False

        # Order tracking
        self._orders: Dict[str, _TrackedOrder] = {}      # cb_order_id -> _TrackedOrder
        self._cl_to_cb: Dict[str, str] = {}              # cl_ord_id -> cb_order_id

        # Background tasks
        self._fill_task: Optional[asyncio.Task] = None
        self._ws = None  # reference to active WebSocket for clean shutdown

    def set_report_callback(self, callback: Callable):
        """Set the callback for sending execution reports back to EXCHCONN."""
        self._report_callback = callback

    async def start(self):
        """Initialize the SDK client and start fill-monitoring."""
        self._running = True

        # Initialize REST client
        if COINBASE_API_KEY_NAME and COINBASE_API_SECRET:
            self._client = RESTClient(
                api_key=COINBASE_API_KEY_NAME,
                api_secret=COINBASE_API_SECRET,
            )
            logger.info(f"[{self.name}] REST client initialized (mode={COINBASE_MODE})")
        else:
            logger.warning(
                f"[{self.name}] No API credentials configured. "
                "Order submission will fail until credentials are set."
            )

        # Start fill monitoring
        if COINBASE_MODE == "production":
            self._fill_task = asyncio.create_task(self._ws_user_loop())
        else:
            self._fill_task = asyncio.create_task(self._poll_fills_loop())

        logger.info(f"[{self.name}] Adapter started (mode={COINBASE_MODE})")

    async def stop(self):
        """Stop the adapter and cancel background tasks."""
        self._running = False
        # Close the WebSocket if it is still open
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception as e:
                logger.warning(f"[{self.name}] Error closing WebSocket: {e}")
            self._ws = None
        if self._fill_task:
            self._fill_task.cancel()
            try:
                await self._fill_task
            except asyncio.CancelledError:
                pass
            self._fill_task = None
        logger.info(f"[{self.name}] Adapter stopped")

    # ---- Order submission ----

    async def submit_order(self, fix_msg: FIXMessage):
        """Submit a new order to Coinbase."""
        cl_ord_id = fix_msg.get(Tag.ClOrdID)
        symbol = fix_msg.get(Tag.Symbol)
        side = fix_msg.get(Tag.Side)
        qty = float(fix_msg.get(Tag.OrderQty, "0"))
        ord_type = fix_msg.get(Tag.OrdType)
        price = float(fix_msg.get(Tag.Price, "0"))

        if not self._client:
            await self._send_reject(cl_ord_id, symbol, side, "No API credentials configured")
            return

        product_id = _SYMBOL_TO_PRODUCT.get(symbol)
        if not product_id:
            await self._send_reject(cl_ord_id, symbol, side, f"Unknown symbol: {symbol}")
            return

        cb_side = "BUY" if side == "1" else "SELL"

        # Build order configuration based on type
        try:
            if ord_type == OrdType.Market:
                if cb_side == "BUY":
                    # Market buy requires quote_size (USD amount) — use qty as a base-size workaround
                    response = await asyncio.to_thread(
                        self._client.market_order,
                        client_order_id=cl_ord_id,
                        product_id=product_id,
                        side=cb_side,
                        base_size=str(qty),
                    )
                else:
                    response = await asyncio.to_thread(
                        self._client.market_order,
                        client_order_id=cl_ord_id,
                        product_id=product_id,
                        side=cb_side,
                        base_size=str(qty),
                    )
            elif ord_type == OrdType.Limit:
                response = await asyncio.to_thread(
                    self._client.limit_order_gtc,
                    client_order_id=cl_ord_id,
                    product_id=product_id,
                    side=cb_side,
                    base_size=str(qty),
                    limit_price=str(price),
                )
            else:
                await self._send_reject(cl_ord_id, symbol, side, f"Unsupported order type: {ord_type}")
                return

        except Exception as e:
            logger.error(f"[{self.name}] Order submission failed: {e}")
            await self._send_reject(cl_ord_id, symbol, side, str(e))
            return

        # Parse response
        success = getattr(response, "success", False)
        if not success:
            error_msg = getattr(response, "error_response", None)
            reason = str(error_msg) if error_msg else "Order rejected by Coinbase"
            logger.warning(f"[{self.name}] Order rejected: {reason}")
            await self._send_reject(cl_ord_id, symbol, side, reason)
            return

        # Extract Coinbase order ID
        success_resp = getattr(response, "success_response", response)
        cb_order_id = getattr(success_resp, "order_id", None) or str(success_resp)

        # Track the order
        tracked = _TrackedOrder(cl_ord_id, cb_order_id, symbol, side, qty, ord_type, price)
        self._orders[cb_order_id] = tracked
        self._cl_to_cb[cl_ord_id] = cb_order_id

        logger.info(
            f"[{self.name}] Order accepted: cb_id={cb_order_id} cl={cl_ord_id} "
            f"{symbol} {'BUY' if side == '1' else 'SELL'} {qty} "
            f"{'MKT' if ord_type == OrdType.Market else f'LMT@{price}'}"
        )

        # Send New acknowledgment
        ack = execution_report(
            cl_ord_id=cl_ord_id,
            order_id=cb_order_id,
            exec_type=ExecType.New,
            ord_status=OrdStatus.New,
            symbol=symbol,
            side=side,
            leaves_qty=qty,
            cum_qty=0.0,
            avg_px=0.0,
        )
        await self._send_report(ack)

    # ---- Cancel ----

    async def cancel_order(self, fix_msg: FIXMessage):
        """Cancel an open order on Coinbase."""
        orig_cl_ord_id = fix_msg.get(Tag.OrigClOrdID)
        cl_ord_id = fix_msg.get(Tag.ClOrdID)
        symbol = fix_msg.get(Tag.Symbol)
        side = fix_msg.get(Tag.Side)

        cb_order_id = self._cl_to_cb.get(orig_cl_ord_id)
        if not cb_order_id or cb_order_id not in self._orders:
            await self._send_reject(cl_ord_id, symbol, side, f"Unknown order: {orig_cl_ord_id}")
            return

        tracked = self._orders[cb_order_id]

        if not self._client:
            await self._send_reject(cl_ord_id, symbol, side, "No API credentials configured")
            return

        try:
            response = await asyncio.to_thread(
                self._client.cancel_orders,
                order_ids=[cb_order_id],
            )
        except Exception as e:
            logger.error(f"[{self.name}] Cancel failed: {e}")
            await self._send_reject(cl_ord_id, tracked.symbol, tracked.side, str(e))
            return

        # Check response for success
        results = getattr(response, "results", [])
        if results:
            first = results[0]
            success = getattr(first, "success", False)
            if not success:
                reason = getattr(first, "failure_reason", "Cancel rejected by Coinbase")
                await self._send_reject(cl_ord_id, tracked.symbol, tracked.side, str(reason))
                return

        tracked.is_terminal = True
        tracked.leaves_qty = 0.0

        logger.info(f"[{self.name}] Cancel confirmed: {cb_order_id}")

        report = execution_report(
            cl_ord_id=cl_ord_id,
            order_id=cb_order_id,
            exec_type=ExecType.Canceled,
            ord_status=OrdStatus.Canceled,
            symbol=tracked.symbol,
            side=tracked.side,
            leaves_qty=0.0,
            cum_qty=round(tracked.cum_qty, 8),
            avg_px=round(tracked.avg_px, 8),
        )
        await self._send_report(report)

    # ---- Amend ----

    async def amend_order(self, fix_msg: FIXMessage):
        """Amend (edit) an open order on Coinbase."""
        orig_cl_ord_id = fix_msg.get(Tag.OrigClOrdID)
        cl_ord_id = fix_msg.get(Tag.ClOrdID)
        symbol = fix_msg.get(Tag.Symbol)
        side = fix_msg.get(Tag.Side)
        new_qty = float(fix_msg.get(Tag.OrderQty, "0"))
        new_price = float(fix_msg.get(Tag.Price, "0"))

        cb_order_id = self._cl_to_cb.get(orig_cl_ord_id)
        if not cb_order_id or cb_order_id not in self._orders:
            await self._send_reject(cl_ord_id, symbol, side, f"Unknown order: {orig_cl_ord_id}")
            return

        tracked = self._orders[cb_order_id]

        # Market orders cannot be edited
        if tracked.ord_type == OrdType.Market:
            await self._send_reject(
                cl_ord_id, tracked.symbol, tracked.side,
                "Market orders cannot be amended"
            )
            return

        if not self._client:
            await self._send_reject(cl_ord_id, symbol, side, "No API credentials configured")
            return

        try:
            response = await asyncio.to_thread(
                self._client.edit_order,
                order_id=cb_order_id,
                size=str(new_qty) if new_qty > 0 else None,
                price=str(new_price) if new_price > 0 else None,
            )
        except Exception as e:
            logger.error(f"[{self.name}] Amend failed: {e}")
            await self._send_reject(cl_ord_id, tracked.symbol, tracked.side, str(e))
            return

        success = getattr(response, "success", False)
        if not success:
            errors = getattr(response, "errors", None)
            reason = str(errors) if errors else "Amend rejected by Coinbase"
            await self._send_reject(cl_ord_id, tracked.symbol, tracked.side, reason)
            return

        # Update tracked state
        if new_qty > 0:
            tracked.total_qty = new_qty
            tracked.leaves_qty = max(0.0, new_qty - tracked.cum_qty)
        if new_price > 0:
            tracked.price = new_price

        self._cl_to_cb[cl_ord_id] = cb_order_id
        tracked.cl_ord_id = cl_ord_id

        logger.info(f"[{self.name}] Amend confirmed: {cb_order_id} qty={new_qty} price={new_price}")

        report = execution_report(
            cl_ord_id=cl_ord_id,
            order_id=cb_order_id,
            exec_type=ExecType.Replaced,
            ord_status=OrdStatus.Replaced,
            symbol=tracked.symbol,
            side=tracked.side,
            leaves_qty=round(tracked.leaves_qty, 8),
            cum_qty=round(tracked.cum_qty, 8),
            avg_px=round(tracked.avg_px, 8),
        )
        await self._send_report(report)

    # ---- Fill monitoring: WebSocket (production) ----

    async def _ws_user_loop(self):
        """Connect to authenticated user channel for real-time fill updates."""
        import json
        import websockets

        backoff = 5.0
        max_backoff = 60.0
        product_ids = list(_SYMBOL_TO_PRODUCT.values())

        while self._running:
            ws = None
            try:
                ws = await websockets.connect(COINBASE_WS_USER_URL)
                self._ws = ws
                logger.info(f"[{self.name}] User WS connected")
                backoff = 5.0

                # Send authenticated subscribe
                sub_msg = build_ws_subscribe_message(
                    COINBASE_API_KEY_NAME,
                    COINBASE_API_SECRET,
                    "user",
                    product_ids,
                )
                await ws.send(json.dumps(sub_msg))
                logger.info(f"[{self.name}] Subscribed to user channel")

                # After reconnect, poll REST to catch missed fills
                await self._reconcile_active_orders()

                async for raw in ws:
                    if not self._running:
                        break
                    try:
                        msg = json.loads(raw)
                        await self._handle_user_event(msg)
                    except json.JSONDecodeError as e:
                        logger.warning(f"[{self.name}] Failed to decode WS message: {e}")
                    except Exception as e:
                        logger.error(f"[{self.name}] Error handling user WS: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                logger.warning(
                    f"[{self.name}] User WS disconnected: {e}. "
                    f"Reconnecting in {backoff:.0f}s..."
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
            finally:
                self._ws = None
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception as e:
                        logger.debug(f"[{self.name}] Error closing WebSocket: {e}")

    async def _handle_user_event(self, msg: dict):
        """Process a user channel event for order/fill updates."""
        channel = msg.get("channel")
        if channel != "user":
            return

        events = msg.get("events", [])
        for event in events:
            orders = event.get("orders", [])
            for order_data in orders:
                cb_order_id = order_data.get("order_id", "")
                tracked = self._orders.get(cb_order_id)
                if not tracked:
                    continue

                new_cum_qty = float(order_data.get("cumulative_quantity", 0))
                new_avg_px = float(order_data.get("average_filled_price", 0))
                status = order_data.get("status", "")

                # Compute fill delta
                last_qty = new_cum_qty - tracked.cum_qty
                if last_qty > 1e-10:
                    # Compute last_px from the delta
                    if tracked.cum_qty > 0 and tracked.avg_px > 0:
                        last_px = (
                            (new_cum_qty * new_avg_px - tracked.cum_qty * tracked.avg_px) / last_qty
                        )
                    else:
                        last_px = new_avg_px

                    tracked.cum_qty = new_cum_qty
                    tracked.avg_px = new_avg_px
                    tracked.leaves_qty = max(0.0, tracked.total_qty - new_cum_qty)

                    is_filled = status in ("FILLED",) or tracked.leaves_qty <= 1e-10
                    if is_filled:
                        tracked.leaves_qty = 0.0
                        tracked.is_terminal = True

                    ord_status = OrdStatus.Filled if is_filled else OrdStatus.PartiallyFilled

                    report = execution_report(
                        cl_ord_id=tracked.cl_ord_id,
                        order_id=cb_order_id,
                        exec_type=ExecType.Trade,
                        ord_status=ord_status,
                        symbol=tracked.symbol,
                        side=tracked.side,
                        leaves_qty=round(tracked.leaves_qty, 8),
                        cum_qty=round(tracked.cum_qty, 8),
                        avg_px=round(tracked.avg_px, 8),
                        last_px=round(last_px, 8),
                        last_qty=round(last_qty, 8),
                    )
                    await self._send_report(report)

                    logger.info(
                        f"[{self.name}] Fill (WS): {cb_order_id} "
                        f"{round(last_qty, 8)}@{round(last_px, 8)} "
                        f"cum={round(tracked.cum_qty, 8)} leaves={round(tracked.leaves_qty, 8)}"
                    )

                elif status in _TERMINAL_STATUSES and not tracked.is_terminal:
                    tracked.is_terminal = True
                    tracked.leaves_qty = 0.0
                    if status == "CANCELLED":
                        report = execution_report(
                            cl_ord_id=tracked.cl_ord_id,
                            order_id=cb_order_id,
                            exec_type=ExecType.Canceled,
                            ord_status=OrdStatus.Canceled,
                            symbol=tracked.symbol,
                            side=tracked.side,
                            leaves_qty=0.0,
                            cum_qty=round(tracked.cum_qty, 8),
                            avg_px=round(tracked.avg_px, 8),
                        )
                        await self._send_report(report)

    # ---- Fill monitoring: REST polling (sandbox) ----

    async def _poll_fills_loop(self):
        """Poll REST for order status updates (sandbox fallback, every 2s)."""
        while self._running:
            try:
                await asyncio.sleep(2.0)
                await self._reconcile_active_orders()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{self.name}] Poll fills error: {e}")

    async def _reconcile_active_orders(self):
        """Query REST for all active (non-terminal) orders and process updates."""
        if not self._client:
            return

        active_orders = [
            (cb_id, t) for cb_id, t in self._orders.items() if not t.is_terminal
        ]

        for cb_order_id, tracked in active_orders:
            try:
                response = await asyncio.to_thread(
                    self._client.get_order,
                    order_id=cb_order_id,
                )

                order_data = getattr(response, "order", response)
                new_cum_qty = float(getattr(order_data, "filled_size", 0) or 0)
                new_avg_px = float(getattr(order_data, "average_filled_price", 0) or 0)
                status = getattr(order_data, "status", "")

                last_qty = new_cum_qty - tracked.cum_qty
                if last_qty > 1e-10:
                    if tracked.cum_qty > 0 and tracked.avg_px > 0:
                        last_px = (
                            (new_cum_qty * new_avg_px - tracked.cum_qty * tracked.avg_px) / last_qty
                        )
                    else:
                        last_px = new_avg_px

                    tracked.cum_qty = new_cum_qty
                    tracked.avg_px = new_avg_px
                    tracked.leaves_qty = max(0.0, tracked.total_qty - new_cum_qty)

                    is_filled = status == "FILLED" or tracked.leaves_qty <= 1e-10
                    if is_filled:
                        tracked.leaves_qty = 0.0
                        tracked.is_terminal = True

                    ord_status = OrdStatus.Filled if is_filled else OrdStatus.PartiallyFilled

                    report = execution_report(
                        cl_ord_id=tracked.cl_ord_id,
                        order_id=cb_order_id,
                        exec_type=ExecType.Trade,
                        ord_status=ord_status,
                        symbol=tracked.symbol,
                        side=tracked.side,
                        leaves_qty=round(tracked.leaves_qty, 8),
                        cum_qty=round(tracked.cum_qty, 8),
                        avg_px=round(tracked.avg_px, 8),
                        last_px=round(last_px, 8),
                        last_qty=round(last_qty, 8),
                    )
                    await self._send_report(report)

                    logger.info(
                        f"[{self.name}] Fill (REST): {cb_order_id} "
                        f"{round(last_qty, 8)}@{round(last_px, 8)} "
                        f"cum={round(tracked.cum_qty, 8)} leaves={round(tracked.leaves_qty, 8)}"
                    )

                elif status in _TERMINAL_STATUSES and not tracked.is_terminal:
                    tracked.is_terminal = True
                    # Send appropriate exec report for non-fill terminal statuses
                    if status == "CANCELLED":
                        et, os_ = ExecType.Canceled, OrdStatus.Canceled
                    else:  # EXPIRED, FAILED
                        et, os_ = ExecType.Rejected, OrdStatus.Rejected
                    report = execution_report(
                        cl_ord_id=tracked.cl_ord_id,
                        order_id=tracked.cb_order_id or "",
                        exec_type=et,
                        ord_status=os_,
                        symbol=tracked.symbol,
                        side=tracked.side,
                        leaves_qty=0.0,
                        cum_qty=round(tracked.cum_qty, 8),
                        avg_px=round(tracked.avg_px, 8),
                        text=f"Order {status} (REST poll)",
                    )
                    await self._send_report(report)
                    logger.info(f"[{self.name}] Terminal status (REST): {cb_order_id} {status}")

            except Exception as e:
                logger.error(f"[{self.name}] REST poll error for {cb_order_id}: {e}")

    # ---- Helpers ----

    async def _send_report(self, report: FIXMessage):
        """Send an execution report via the callback."""
        if self._report_callback:
            report.set(Tag.ExDestination, self.name)
            try:
                await self._report_callback(report)
            except Exception as e:
                logger.error(f"[{self.name}] Failed to send report: {e}")

    async def _send_reject(self, cl_ord_id: str, symbol: str, side: str, reason: str):
        """Send a Rejected execution report."""
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
