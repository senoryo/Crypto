"""
Simulated HTX (formerly Huobi) Exchange Adapter.

Accepts orders, generates execution reports with realistic delays and partial fills.
Order IDs are prefixed with "HTX-".
HTX characteristics: widest spreads (0.15% jitter), slowest fills (0.8-3.0s delay),
largest price variation (+-0.03%).
"""

import asyncio
import itertools
import logging
import random
import time
from typing import Callable, Dict, Optional

from shared.fix_protocol import (
    FIXMessage, Tag, MsgType, ExecType, OrdStatus, OrdType,
    execution_report,
)
from shared.config import EXCHANGES

logger = logging.getLogger(__name__)

# Base simulated prices (same across exchanges)
BASE_PRICES = {
    "BTC/USD": 67500.0,
    "ETH/USD": 3450.0,
    "SOL/USD": 178.0,
    "ADA/USD": 0.72,
    "DOGE/USD": 0.165,
}

# HTX has the widest spreads
PRICE_JITTER_PCT = 0.0015  # 0.15%


class SimulatedOrder:
    """Tracks state of a simulated order on the exchange."""

    def __init__(self, order_id: str, cl_ord_id: str, symbol: str, side: str,
                 qty: float, ord_type: str, price: float = 0.0):
        self.order_id = order_id
        self.cl_ord_id = cl_ord_id
        self.symbol = symbol
        self.side = side
        self.total_qty = qty
        self.ord_type = ord_type
        self.price = price
        self.cum_qty = 0.0
        self.avg_px = 0.0
        self.leaves_qty = qty
        self.is_active = True
        self.fill_chunks = random.randint(1, 3)
        self.fills_done = 0


class HTXSimulator:
    """Simulated HTX (formerly Huobi) exchange that processes orders with realistic behavior."""

    def __init__(self):
        self.name = "HTX"
        self.prefix = "HTX"
        self._order_counter = itertools.count(1)
        self._orders: Dict[str, SimulatedOrder] = {}
        self._cl_to_order: Dict[str, str] = {}
        self._report_callback: Optional[Callable] = None
        self._current_prices: Dict[str, float] = dict(BASE_PRICES)
        self._price_task: Optional[asyncio.Task] = None
        self._fill_tasks: Dict[str, asyncio.Task] = {}
        self._running = False

    def set_report_callback(self, callback: Callable):
        """Set the callback for sending execution reports back to EXCHCONN."""
        self._report_callback = callback

    def _next_order_id(self) -> str:
        n = next(self._order_counter)
        return f"{self.prefix}-{n:06d}"

    def _get_current_price(self, symbol: str) -> float:
        return self._current_prices.get(symbol, 0.0)

    async def start(self):
        """Start the price simulation loop."""
        self._running = True
        self._price_task = asyncio.create_task(self._price_jitter_loop())
        logger.info(f"[{self.name}] Simulator started")

    async def stop(self):
        """Stop the simulator and cancel pending tasks."""
        self._running = False
        if self._price_task:
            self._price_task.cancel()
            try:
                await self._price_task
            except asyncio.CancelledError:
                pass
        for task in list(self._fill_tasks.values()):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._fill_tasks.clear()
        logger.info(f"[{self.name}] Simulator stopped")

    async def _price_jitter_loop(self):
        """Randomly walk prices every 0.5s to simulate market movement."""
        while self._running:
            try:
                await asyncio.sleep(0.5)
                for symbol in self._current_prices:
                    base = BASE_PRICES[symbol]
                    jitter = base * PRICE_JITTER_PCT
                    self._current_prices[symbol] += random.uniform(-jitter, jitter)
                    self._current_prices[symbol] = max(
                        base * 0.5,
                        min(base * 1.5, self._current_prices[symbol])
                    )
                await self._check_limit_fills()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{self.name}] Price jitter error: {e}")

    async def _check_limit_fills(self):
        """Check if any limit orders can be filled at current prices."""
        for order_id, order in list(self._orders.items()):
            if not order.is_active or order.ord_type != OrdType.Limit:
                continue
            if order_id in self._fill_tasks:
                continue

            current_price = self._get_current_price(order.symbol)
            should_fill = False

            if order.side == "1" and current_price <= order.price:
                should_fill = True
            elif order.side == "2" and current_price >= order.price:
                should_fill = True

            if should_fill:
                task = asyncio.create_task(
                    self._execute_fill(order, fill_price=order.price)
                )
                self._fill_tasks[order_id] = task

    async def _send_report(self, report: FIXMessage):
        """Send an execution report via the callback."""
        if self._report_callback:
            report.set(Tag.ExDestination, self.name)
            try:
                await self._report_callback(report)
            except Exception as e:
                logger.error(f"[{self.name}] Failed to send report: {e}")

    async def submit_order(self, fix_msg: FIXMessage):
        """Accept a new order and begin processing."""
        cl_ord_id = fix_msg.get(Tag.ClOrdID)
        symbol = fix_msg.get(Tag.Symbol)
        side = fix_msg.get(Tag.Side)
        qty = float(fix_msg.get(Tag.OrderQty, "0"))
        ord_type = fix_msg.get(Tag.OrdType)
        price = float(fix_msg.get(Tag.Price, "0"))

        # Reject orders for unsupported symbols (zero price)
        sim_price = self._get_current_price(symbol)
        if sim_price <= 0:
            reject = execution_report(
                cl_ord_id=cl_ord_id,
                order_id="NONE",
                exec_type=ExecType.Rejected,
                ord_status=OrdStatus.Rejected,
                symbol=symbol,
                side=side,
                leaves_qty=0.0,
                cum_qty=0.0,
                avg_px=0.0,
                text=f"Unsupported symbol: {symbol}",
            )
            await self._send_report(reject)
            return

        order_id = self._next_order_id()
        order = SimulatedOrder(order_id, cl_ord_id, symbol, side, qty, ord_type, price)
        self._orders[order_id] = order
        self._cl_to_order[cl_ord_id] = order_id

        logger.info(
            f"[{self.name}] New order: {order_id} cl={cl_ord_id} "
            f"{symbol} {'BUY' if side == '1' else 'SELL'} {qty} "
            f"{'MKT' if ord_type == OrdType.Market else f'LMT@{price}'}"
        )

        ack = execution_report(
            cl_ord_id=cl_ord_id,
            order_id=order_id,
            exec_type=ExecType.New,
            ord_status=OrdStatus.New,
            symbol=symbol,
            side=side,
            leaves_qty=qty,
            cum_qty=0.0,
            avg_px=0.0,
        )
        await self._send_report(ack)

        if ord_type == OrdType.Market:
            fill_price = self._get_current_price(symbol)
            task = asyncio.create_task(self._execute_fill(order, fill_price))
            self._fill_tasks[order_id] = task

    async def _execute_fill(self, order: SimulatedOrder, fill_price: float):
        """Execute fills for an order, potentially in multiple chunks."""
        try:
            chunks = order.fill_chunks - order.fills_done
            if chunks <= 0:
                chunks = 1

            for i in range(chunks):
                if not order.is_active:
                    break

                # HTX: slowest fills (0.8-3.0s)
                delay = random.uniform(0.8, 3.0)
                await asyncio.sleep(delay)

                if not order.is_active:
                    break

                if i == chunks - 1:
                    fill_qty = order.leaves_qty
                else:
                    fill_qty = round(order.leaves_qty * random.uniform(0.3, 0.6), 8)
                    fill_qty = min(fill_qty, order.leaves_qty)

                if fill_qty <= 0:
                    break

                # HTX: widest price variation (+-0.03%)
                chunk_price = fill_price * (1 + random.uniform(-0.0003, 0.0003))
                chunk_price = round(chunk_price, 8)

                old_cum = order.cum_qty
                order.cum_qty += fill_qty
                order.avg_px = (
                    (old_cum * order.avg_px + fill_qty * chunk_price) / order.cum_qty
                    if order.cum_qty > 0 else 0.0
                )
                order.leaves_qty -= fill_qty
                order.leaves_qty = max(0.0, order.leaves_qty)
                order.fills_done += 1

                is_final = order.leaves_qty <= 1e-10
                if is_final:
                    order.leaves_qty = 0.0
                    order.is_active = False

                ord_status = OrdStatus.Filled if is_final else OrdStatus.PartiallyFilled
                exec_type = ExecType.Trade

                report = execution_report(
                    cl_ord_id=order.cl_ord_id,
                    order_id=order.order_id,
                    exec_type=exec_type,
                    ord_status=ord_status,
                    symbol=order.symbol,
                    side=order.side,
                    leaves_qty=round(order.leaves_qty, 8),
                    cum_qty=round(order.cum_qty, 8),
                    avg_px=round(order.avg_px, 8),
                    last_px=chunk_price,
                    last_qty=round(fill_qty, 8),
                )
                await self._send_report(report)

                logger.info(
                    f"[{self.name}] Fill: {order.order_id} "
                    f"{round(fill_qty, 8)}@{chunk_price} "
                    f"cum={round(order.cum_qty, 8)} leaves={round(order.leaves_qty, 8)}"
                )

                if is_final:
                    break

        except asyncio.CancelledError:
            logger.debug(f"[{self.name}] Fill task cancelled for {order.order_id}")
        except Exception as e:
            logger.error(f"[{self.name}] Fill error for {order.order_id}: {e}")
        finally:
            self._fill_tasks.pop(order.order_id, None)

    async def cancel_order(self, fix_msg: FIXMessage):
        """Cancel an open order."""
        orig_cl_ord_id = fix_msg.get(Tag.OrigClOrdID)
        cl_ord_id = fix_msg.get(Tag.ClOrdID)
        symbol = fix_msg.get(Tag.Symbol)
        side = fix_msg.get(Tag.Side)

        order_id = self._cl_to_order.get(orig_cl_ord_id)
        if not order_id or order_id not in self._orders:
            reject = execution_report(
                cl_ord_id=cl_ord_id,
                order_id=order_id or "UNKNOWN",
                exec_type=ExecType.Rejected,
                ord_status=OrdStatus.Rejected,
                symbol=symbol,
                side=side,
                leaves_qty=0.0,
                cum_qty=0.0,
                avg_px=0.0,
                text=f"Unknown order: {orig_cl_ord_id}",
            )
            await self._send_report(reject)
            return

        order = self._orders[order_id]

        if not order.is_active:
            reject = execution_report(
                cl_ord_id=cl_ord_id,
                order_id=order_id,
                exec_type=ExecType.Rejected,
                ord_status=OrdStatus.Rejected,
                symbol=order.symbol,
                side=order.side,
                leaves_qty=0.0,
                cum_qty=order.cum_qty,
                avg_px=order.avg_px,
                text="Order not active",
            )
            await self._send_report(reject)
            return

        if order_id in self._fill_tasks:
            self._fill_tasks[order_id].cancel()
            try:
                await self._fill_tasks[order_id]
            except asyncio.CancelledError:
                pass
            self._fill_tasks.pop(order_id, None)

        order.is_active = False
        old_leaves = order.leaves_qty
        order.leaves_qty = 0.0

        logger.info(f"[{self.name}] Cancel: {order_id} (leaves was {old_leaves})")

        report = execution_report(
            cl_ord_id=cl_ord_id,
            order_id=order_id,
            exec_type=ExecType.Canceled,
            ord_status=OrdStatus.Canceled,
            symbol=order.symbol,
            side=order.side,
            leaves_qty=0.0,
            cum_qty=round(order.cum_qty, 8),
            avg_px=round(order.avg_px, 8),
        )
        await self._send_report(report)

    async def amend_order(self, fix_msg: FIXMessage):
        """Amend (cancel/replace) an open order."""
        orig_cl_ord_id = fix_msg.get(Tag.OrigClOrdID)
        cl_ord_id = fix_msg.get(Tag.ClOrdID)
        symbol = fix_msg.get(Tag.Symbol)
        side = fix_msg.get(Tag.Side)
        new_qty = float(fix_msg.get(Tag.OrderQty, "0"))
        new_price = float(fix_msg.get(Tag.Price, "0"))

        order_id = self._cl_to_order.get(orig_cl_ord_id)
        if not order_id or order_id not in self._orders:
            reject = execution_report(
                cl_ord_id=cl_ord_id,
                order_id=order_id or "UNKNOWN",
                exec_type=ExecType.Rejected,
                ord_status=OrdStatus.Rejected,
                symbol=symbol,
                side=side,
                leaves_qty=0.0,
                cum_qty=0.0,
                avg_px=0.0,
                text=f"Unknown order: {orig_cl_ord_id}",
            )
            await self._send_report(reject)
            return

        order = self._orders[order_id]

        if not order.is_active:
            reject = execution_report(
                cl_ord_id=cl_ord_id,
                order_id=order_id,
                exec_type=ExecType.Rejected,
                ord_status=OrdStatus.Rejected,
                symbol=order.symbol,
                side=order.side,
                leaves_qty=0.0,
                cum_qty=order.cum_qty,
                avg_px=order.avg_px,
                text="Order not active",
            )
            await self._send_report(reject)
            return

        if order_id in self._fill_tasks:
            self._fill_tasks[order_id].cancel()
            try:
                await self._fill_tasks[order_id]
            except asyncio.CancelledError:
                pass
            self._fill_tasks.pop(order_id, None)

        if new_qty > 0:
            order.total_qty = new_qty
            order.leaves_qty = max(0.0, new_qty - order.cum_qty)
        if new_price > 0:
            order.price = new_price

        self._cl_to_order[cl_ord_id] = order_id
        order.cl_ord_id = cl_ord_id

        logger.info(
            f"[{self.name}] Amend: {order_id} new_qty={new_qty} new_price={new_price}"
        )

        report = execution_report(
            cl_ord_id=cl_ord_id,
            order_id=order_id,
            exec_type=ExecType.Replaced,
            ord_status=OrdStatus.Replaced,
            symbol=order.symbol,
            side=order.side,
            leaves_qty=round(order.leaves_qty, 8),
            cum_qty=round(order.cum_qty, 8),
            avg_px=round(order.avg_px, 8),
        )
        report.set(Tag.OrderQty, round(order.total_qty, 8))
        report.set(Tag.Price, round(order.price, 8))
        await self._send_report(report)

        if order.is_active and order.leaves_qty > 0:
            if order.ord_type == OrdType.Market:
                fill_price = self._get_current_price(order.symbol)
                self._fill_tasks[order_id] = asyncio.create_task(
                    self._execute_fill(order, fill_price)
                )
