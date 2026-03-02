"""
AlgoEngine — Core algo trading engine.

Connects to MKTDATA and OM via WebSocket, manages strategy instances,
routes execution reports to strategies, and provides a control WSServer
on port 8086 for external commands (start/stop/pause algos, query status).

Safety controls:
- Rate limiter: configurable max child orders/sec across all strategies
- Max concurrent strategies (default 10)
- Max aggregate notional (default 10M USD)
- Max total orders per algo (default 10,000)
- Dead man's switch: auto-pause all strategies on MKTDATA/OM disconnect
- Global kill: cancel_all() cancels all children across all strategies
- Heartbeat tracking: warn if no market data for 5 seconds
"""

import asyncio
import itertools
import json
import logging
import time

from algo.parent_order import ParentOrder, ChildOrder, ParentState
from algo.strategies.base import BaseStrategy
from shared.config import HOST, PORTS
from shared.fix_protocol import (
    FIXMessage, Tag, MsgType, ExecType, OrdStatus,
    new_order_single, cancel_request,
)
from shared.logging_config import log_recv, log_send
from shared.ws_transport import WSServer, WSClient

logger = logging.getLogger("ALGO")

# --- Constants ---
DEFAULT_ALGO_PORT = 8086
DEFAULT_MAX_ORDERS_PER_SECOND = 50
DEFAULT_MAX_CONCURRENT_ALGOS = 10
DEFAULT_MAX_AGGREGATE_NOTIONAL = 10_000_000
DEFAULT_MAX_TOTAL_ORDERS_PER_ALGO = 10_000
DEFAULT_MAX_ALGO_RUNTIME = 86400  # 24 hours
MKTDATA_HEARTBEAT_TIMEOUT = 5.0  # seconds


class RateLimiter:
    """Sliding-window rate limiter for child order submission."""

    def __init__(self, max_per_second: int = DEFAULT_MAX_ORDERS_PER_SECOND):
        self._max = max_per_second
        self._timestamps: list[float] = []

    def allow(self) -> bool:
        """Check whether a new order is allowed under the rate limit."""
        now = time.time()
        cutoff = now - 1.0
        self._timestamps = [t for t in self._timestamps if t > cutoff]
        if len(self._timestamps) >= self._max:
            return False
        self._timestamps.append(now)
        return True

    def reset(self) -> None:
        self._timestamps.clear()


class AlgoEngine:
    """Core algorithmic trading engine."""

    def __init__(
        self,
        mktdata_url: str | None = None,
        om_url: str | None = None,
        control_port: int = DEFAULT_ALGO_PORT,
        max_concurrent_algos: int = DEFAULT_MAX_CONCURRENT_ALGOS,
        max_aggregate_notional: float = DEFAULT_MAX_AGGREGATE_NOTIONAL,
        max_orders_per_second: int = DEFAULT_MAX_ORDERS_PER_SECOND,
        max_total_orders_per_algo: int = DEFAULT_MAX_TOTAL_ORDERS_PER_ALGO,
        max_algo_runtime: float = DEFAULT_MAX_ALGO_RUNTIME,
    ):
        # Resolve URLs
        _mktdata_url = mktdata_url or f"ws://{HOST}:{PORTS['MKTDATA']}"
        _om_url = om_url or f"ws://{HOST}:{PORTS['OM']}"

        # WebSocket connections
        self._mktdata_client = WSClient(_mktdata_url, name="ALGO->MKTDATA")
        self._om_client = WSClient(_om_url, name="ALGO->OM")
        self._control_port = control_port
        self._server = WSServer(HOST, control_port, name="ALGO-Control")

        # Market data cache: symbol -> latest tick dict
        self._market_data: dict[str, dict] = {}
        self._last_mktdata_time: float = time.time()

        # Strategy management
        self._strategies: dict[str, BaseStrategy] = {}  # parent_id -> strategy
        self._parent_orders: dict[str, ParentOrder] = {}  # parent_id -> ParentOrder
        self._child_to_parent: dict[str, str] = {}  # child_cl_ord_id -> parent_id
        self._order_counter = itertools.count(1)

        # Global limits
        self.max_concurrent_algos = max_concurrent_algos
        self.max_aggregate_notional = max_aggregate_notional
        self.max_orders_per_second = max_orders_per_second
        self.max_total_orders_per_algo = max_total_orders_per_algo
        self.max_algo_runtime = max_algo_runtime

        # Safety controls
        self._rate_limiter = RateLimiter(max_orders_per_second)
        self._mktdata_connected = False
        self._om_connected = False
        self._running = False

        # Cancel ID counter for generating unique cancel request IDs
        self._cancel_counter = itertools.count(1)

        # Fill deduplication: set of processed ExecIDs
        self._processed_exec_ids: set[str] = set()

        # Deadline enforcement tasks: parent_id -> asyncio.Task
        self._deadline_tasks: dict[str, asyncio.Task] = {}

        # Register handlers
        self._mktdata_client.on_message(self._on_mktdata_message)
        self._om_client.on_message(self._on_om_message)
        self._server.on_message(self._on_control_message)

        # Strategy registry: algo_type -> strategy class
        self._strategy_registry: dict[str, type] = {}

        # Auto-register built-in strategies
        self._register_builtins()

    def _register_builtins(self) -> None:
        """Auto-register built-in strategy types if available."""
        try:
            from algo.strategies.sor import SmartOrderRouter
            self._strategy_registry["SOR"] = SmartOrderRouter
        except ImportError:
            pass
        try:
            from algo.strategies.vwap import VWAPStrategy
            self._strategy_registry["VWAP"] = VWAPStrategy
        except ImportError:
            pass
        try:
            from algo.strategies.twap import TWAPStrategy
            self._strategy_registry["TWAP"] = TWAPStrategy
        except ImportError:
            pass
        try:
            from algo.strategies.is_strategy import ISStrategy
            self._strategy_registry["IS"] = ISStrategy
        except ImportError:
            pass

    # --- Lifecycle ---

    async def start(self) -> None:
        """Start the algo engine: connect to MKTDATA, OM, start control server."""
        logger.info("=" * 60)
        logger.info("Algo Engine starting...")
        logger.info(f"  Control server: port {self._control_port}")
        logger.info("=" * 60)

        self._running = True

        # Start control server
        await self._server.start()

        # Connect to MKTDATA and OM
        await asyncio.gather(
            self._mktdata_client.connect(retry=True),
            self._om_client.connect(retry=True),
        )
        self._mktdata_connected = True
        self._om_connected = True
        self._last_mktdata_time = time.time()

        # Listen on all connections + run heartbeat monitor
        await asyncio.gather(
            self._mktdata_client.listen(),
            self._om_client.listen(),
            self._heartbeat_monitor(),
        )

    async def stop(self) -> None:
        """Stop the algo engine: cancel all algos, disconnect."""
        logger.info("Algo Engine stopping...")
        self._running = False
        # Cancel all deadline tasks
        for task in self._deadline_tasks.values():
            if not task.done():
                task.cancel()
        self._deadline_tasks.clear()
        await self.kill_all()
        await self._mktdata_client.close()
        await self._om_client.close()
        await self._server.stop()
        logger.info("Algo Engine stopped")

    # --- Strategy registry ---

    def register_strategy(self, name: str, strategy_class: type) -> None:
        """Register a strategy class for an algo type."""
        self._strategy_registry[name] = strategy_class

    # --- Algo management ---

    async def submit_algo(
        self,
        algo_type: str,
        symbol: str,
        side: str,
        qty: float,
        params: dict | None = None,
    ) -> str | None:
        """
        Start a new algo execution.

        Returns the parent_order_id on success, or None if rejected.
        """
        params = params or {}

        # Validate algo type
        strategy_class = self._strategy_registry.get(algo_type)
        if strategy_class is None:
            logger.warning(f"Unknown algo type: {algo_type}")
            return None

        # Validate quantity
        if qty <= 0:
            logger.warning(f"Invalid quantity: {qty}")
            return None

        # Check global limits
        if not self._check_global_limits_for_new(symbol, qty):
            return None

        if not self._om_connected:
            logger.warning("Cannot start algo: OM is disconnected")
            return None

        # Generate parent ID
        n = next(self._order_counter)
        parent_id = f"ALGO-{algo_type}-{n:03d}"

        # Capture arrival price from market data
        tick = self._market_data.get(symbol, {})
        bid = tick.get("bid", tick.get("price", 0))
        ask = tick.get("ask", tick.get("price", 0))
        if bid and ask:
            arrival_price = (float(bid) + float(ask)) / 2.0
        elif bid:
            arrival_price = float(bid)
        elif ask:
            arrival_price = float(ask)
        else:
            arrival_price = 0.0

        # Create parent order
        parent_order = ParentOrder(
            parent_id=parent_id,
            symbol=symbol,
            side=side,
            total_qty=qty,
            algo_type=algo_type,
            params=params,
            arrival_price=arrival_price,
        )
        self._parent_orders[parent_id] = parent_order

        # Create strategy instance
        strategy = strategy_class(self, params)
        self._strategies[parent_id] = strategy

        # Start the strategy
        logger.info(
            f"Starting algo {parent_id}: {algo_type} {symbol} "
            f"side={side} qty={qty} arrival_px={arrival_price}"
        )
        await strategy.start(parent_order)

        # Schedule deadline enforcement
        self._deadline_tasks[parent_id] = asyncio.create_task(
            self._enforce_deadline(parent_id, self.max_algo_runtime)
        )

        return parent_id

    async def pause_algo(self, parent_order_id: str) -> bool:
        """Pause a running algo. Returns True if found."""
        strategy = self._strategies.get(parent_order_id)
        if strategy is None:
            logger.warning(f"Cannot pause: unknown algo {parent_order_id}")
            return False
        logger.info(f"Pausing algo {parent_order_id}")
        await strategy.pause()
        return True

    async def resume_algo(self, parent_order_id: str) -> bool:
        """Resume a paused algo. Returns True if found."""
        strategy = self._strategies.get(parent_order_id)
        if strategy is None:
            logger.warning(f"Cannot resume: unknown algo {parent_order_id}")
            return False
        logger.info(f"Resuming algo {parent_order_id}")
        await strategy.resume()
        return True

    async def cancel_algo(self, parent_order_id: str) -> bool:
        """Cancel a running algo. Returns True if found."""
        strategy = self._strategies.get(parent_order_id)
        if strategy is None:
            logger.warning(f"Cannot cancel: unknown algo {parent_order_id}")
            return False
        logger.info(f"Cancelling algo {parent_order_id}")
        # Cancel deadline task if present
        deadline_task = self._deadline_tasks.pop(parent_order_id, None)
        if deadline_task and not deadline_task.done():
            deadline_task.cancel()
        await strategy.stop()
        return True

    async def kill_all(self) -> None:
        """Emergency kill switch: cancel all child orders across all algos."""
        logger.warning("GLOBAL KILL: cancelling all strategies")
        for parent_id in list(self._strategies.keys()):
            strategy = self._strategies.get(parent_id)
            if strategy:
                try:
                    await strategy.stop()
                except Exception as e:
                    logger.error(f"Error stopping {parent_id}: {e}")

    async def _enforce_deadline(self, parent_id: str, timeout: float) -> None:
        """Cancel an algo if it exceeds its maximum runtime."""
        try:
            await asyncio.sleep(timeout)
            strategy = self._strategies.get(parent_id)
            if strategy and strategy._active:
                logger.warning(
                    f"Algo {parent_id} exceeded max runtime "
                    f"({timeout}s) — auto-cancelling"
                )
                await strategy.stop()
        except asyncio.CancelledError:
            pass
        finally:
            self._deadline_tasks.pop(parent_id, None)

    # --- Child order submission (called by strategies) ---

    async def send_child_order(self, child: ChildOrder) -> bool:
        """
        Send a child order to OM via FIX.

        Called by BaseStrategy.submit_child_order(). Returns True if sent.
        """
        # Rate limit check
        if not self._check_rate_limit():
            logger.warning(
                f"Rate limit exceeded: child order {child.cl_ord_id} blocked"
            )
            return False

        if not self._om_connected:
            logger.warning(
                f"OM disconnected: child order {child.cl_ord_id} blocked"
            )
            return False

        # Check max orders per algo
        parent = self._parent_orders.get(child.parent_id)
        if parent and len(parent.child_orders) > self.max_total_orders_per_algo:
            logger.warning(
                f"Max orders per algo ({self.max_total_orders_per_algo}) "
                f"exceeded for {child.parent_id}"
            )
            return False

        # Register child -> parent mapping
        self._child_to_parent[child.cl_ord_id] = child.parent_id

        # Build and send FIX NewOrderSingle
        fix_msg = new_order_single(
            cl_ord_id=child.cl_ord_id,
            symbol=child.symbol,
            side=child.side,
            qty=child.qty,
            ord_type=child.ord_type,
            price=child.price,
            exchange=child.exchange,
        )
        fix_json = fix_msg.to_json()
        log_send(
            logger, "OM",
            f"FIX NewOrderSingle cl={child.cl_ord_id} "
            f"{child.symbol} qty={child.qty} px={child.price} "
            f"ex={child.exchange}",
            fix_json,
        )
        try:
            await self._om_client.send(fix_json)
            return True
        except Exception as e:
            logger.error(f"Failed to send child order {child.cl_ord_id}: {e}")
            return False

    async def cancel_child_order(self, child: ChildOrder) -> bool:
        """Send a cancel request to OM for a child order."""
        if not self._om_connected:
            logger.warning(
                f"OM disconnected: cannot cancel {child.cl_ord_id}"
            )
            return False

        cancel_id = f"CXL-{next(self._cancel_counter)}"
        fix_msg = cancel_request(
            cl_ord_id=cancel_id,
            orig_cl_ord_id=child.cl_ord_id,
            symbol=child.symbol,
            side=child.side,
        )
        fix_json = fix_msg.to_json()
        log_send(
            logger, "OM",
            f"FIX CancelRequest cl={cancel_id} orig={child.cl_ord_id}",
            fix_json,
        )
        try:
            await self._om_client.send(fix_json)
            return True
        except Exception as e:
            logger.error(f"Failed to cancel {child.cl_ord_id}: {e}")
            return False

    # --- Rate limiting and global limits ---

    def _check_rate_limit(self) -> bool:
        """Per-second order rate check."""
        return self._rate_limiter.allow()

    def _check_global_limits_for_new(self, symbol: str, qty: float) -> bool:
        """Check global limits before starting a new algo."""
        # Max concurrent algos
        active_count = sum(
            1 for s in self._strategies.values()
            if s._active and s.parent_order
            and s.parent_order.state not in (ParentState.DONE, ParentState.CANCELLED)
        )
        if active_count >= self.max_concurrent_algos:
            logger.warning(
                f"Cannot start algo: max concurrent algos "
                f"({self.max_concurrent_algos}) reached"
            )
            return False

        # Max aggregate notional: estimate notional from market data
        tick = self._market_data.get(symbol, {})
        price = tick.get("mid", tick.get("bid", tick.get("ask", tick.get("price", 0))))
        if price:
            new_notional = float(price) * qty
            existing_notional = self._aggregate_notional()
            if existing_notional + new_notional > self.max_aggregate_notional:
                logger.warning(
                    f"Cannot start algo: aggregate notional "
                    f"({existing_notional + new_notional:.2f}) exceeds limit "
                    f"({self.max_aggregate_notional:.2f})"
                )
                return False

        return True

    def _aggregate_notional(self) -> float:
        """Calculate total notional across all active algos."""
        total = 0.0
        for parent in self._parent_orders.values():
            if parent.state in (ParentState.DONE, ParentState.CANCELLED):
                continue
            tick = self._market_data.get(parent.symbol, {})
            price = tick.get("mid", tick.get("bid", tick.get("ask", tick.get("price", 0))))
            if price:
                total += float(price) * parent.remaining_qty()
        return total

    # --- Market data handling ---

    async def _on_mktdata_message(self, message: str) -> None:
        """Process incoming market data from MKTDATA."""
        self._last_mktdata_time = time.time()
        if not self._mktdata_connected:
            self._mktdata_connected = True
            logger.info("MKTDATA connection restored")
            await self._resume_all_paused_by_disconnect()

        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return

        symbol = data.get("symbol", "")
        if symbol:
            self._market_data[symbol] = data

            # Fan out tick to all active strategies for matching symbols
            for pid, strategy in self._strategies.items():
                if (
                    strategy._active
                    and not strategy._paused
                    and strategy.parent_order
                    and strategy.parent_order.symbol == symbol
                ):
                    try:
                        await strategy.on_tick(symbol, data)
                    except Exception as e:
                        logger.error(f"Strategy {pid} tick error: {e}")

    # --- Execution report handling ---

    async def _on_om_message(self, message: str) -> None:
        """Process incoming execution reports from OM."""
        if not self._om_connected:
            self._om_connected = True
            logger.info("OM connection restored")
            await self._resume_all_paused_by_disconnect()

        try:
            fix_msg = FIXMessage.from_json(message)
        except (json.JSONDecodeError, KeyError):
            return

        if fix_msg.msg_type != MsgType.ExecutionReport:
            return

        cl_ord_id = fix_msg.get(Tag.ClOrdID)
        exec_type = fix_msg.get(Tag.ExecType)

        # Fill deduplication: skip already-processed exec IDs
        exec_id = fix_msg.get(Tag.ExecID, "")
        if exec_id and exec_type == ExecType.Trade:
            if exec_id in self._processed_exec_ids:
                return  # Already processed (idempotent)
            self._processed_exec_ids.add(exec_id)

        # Find the parent strategy via child order mapping
        parent_id = self._child_to_parent.get(cl_ord_id)
        if parent_id is None:
            return

        strategy = self._strategies.get(parent_id)
        if strategy is None or strategy.parent_order is None:
            return

        child = strategy.parent_order.child_orders.get(cl_ord_id)
        if child is None:
            return

        log_recv(
            logger, "OM",
            f"FIX ExecReport {exec_type} cl={cl_ord_id} parent={parent_id}",
            message,
        )

        if exec_type == ExecType.Trade:
            try:
                fill_qty = float(fix_msg.get(Tag.LastQty, "0"))
                fill_price = float(fix_msg.get(Tag.LastPx, "0"))
            except ValueError:
                return
            strategy.parent_order.process_fill(cl_ord_id, fill_qty, fill_price)
            await strategy.on_fill(child, fill_qty, fill_price)

        elif exec_type == ExecType.New:
            strategy.parent_order.process_child_new(cl_ord_id)

        elif exec_type == ExecType.Canceled:
            strategy.parent_order.process_child_cancelled(cl_ord_id)
            await strategy.on_cancel_ack(child)

        elif exec_type == ExecType.Rejected:
            reason = fix_msg.get(Tag.Text, "unknown")
            strategy.parent_order.process_reject(cl_ord_id, reason)
            await strategy.on_reject(child, reason)

        # Log completion
        if strategy.parent_order.state == ParentState.DONE:
            logger.info(
                f"Algo {parent_id} completed: "
                f"filled={strategy.parent_order.filled_qty} "
                f"avg_px={strategy.parent_order.avg_fill_price:.6f} "
                f"slippage={strategy.parent_order.slippage():.4f}"
            )

    # --- Control server (port 8086) ---

    async def _on_control_message(self, websocket, message: str) -> None:
        """Handle JSON commands from the control server."""
        try:
            cmd = json.loads(message)
        except json.JSONDecodeError:
            await self._server.send_to(
                websocket, json.dumps({"error": "Invalid JSON"})
            )
            return

        action = cmd.get("action", "")
        response: dict

        if action == "submit":
            parent_id = await self.submit_algo(
                algo_type=cmd.get("algo_type", ""),
                symbol=cmd.get("symbol", ""),
                side=cmd.get("side", ""),
                qty=float(cmd.get("qty", 0)),
                params=cmd.get("params", {}),
            )
            if parent_id:
                response = {"status": "ok", "parent_order_id": parent_id}
            else:
                response = {"status": "error", "reason": "Failed to start algo"}

        elif action == "pause":
            ok = await self.pause_algo(cmd.get("parent_order_id", ""))
            response = {"status": "ok" if ok else "error"}

        elif action == "resume":
            ok = await self.resume_algo(cmd.get("parent_order_id", ""))
            response = {"status": "ok" if ok else "error"}

        elif action == "cancel":
            ok = await self.cancel_algo(cmd.get("parent_order_id", ""))
            response = {"status": "ok" if ok else "error"}

        elif action == "kill_all":
            await self.kill_all()
            response = {"status": "ok"}

        elif action == "status":
            response = {"status": "ok", "data": self.get_status()}

        elif action == "algo_status":
            pid = cmd.get("parent_order_id", "")
            s = self.get_algo_status(pid)
            if s:
                response = {"status": "ok", "data": s}
            else:
                response = {"status": "error", "reason": "Not found"}

        else:
            response = {"status": "error", "reason": f"Unknown action: {action}"}

        await self._server.send_to(websocket, json.dumps(response))

    # --- Status ---

    def get_status(self) -> list[dict]:
        """Get status of all active algos with progress."""
        result = []
        for pid, strategy in self._strategies.items():
            if strategy.parent_order:
                result.append(strategy.parent_order.to_dict())
        return result

    def get_algo_status(self, parent_order_id: str) -> dict | None:
        """Get detailed status of a single algo."""
        strategy = self._strategies.get(parent_order_id)
        if strategy is None or strategy.parent_order is None:
            return None
        return strategy.parent_order.to_dict()

    # --- Safety: heartbeat monitoring ---

    async def _heartbeat_monitor(self) -> None:
        """Monitor market data heartbeat; pause all strategies if data stops."""
        while self._running:
            await asyncio.sleep(1.0)
            elapsed = time.time() - self._last_mktdata_time
            if elapsed > MKTDATA_HEARTBEAT_TIMEOUT and self._mktdata_connected:
                logger.warning(
                    f"No market data for {elapsed:.1f}s — "
                    f"pausing all strategies (dead man's switch)"
                )
                self._mktdata_connected = False
                await self._pause_all_for_disconnect()

    async def _pause_all_for_disconnect(self) -> None:
        """Pause all active strategies due to a connectivity issue."""
        for pid, strategy in self._strategies.items():
            if strategy._active and not strategy._paused:
                try:
                    await strategy.pause()
                    logger.info(f"Auto-paused {pid} due to disconnect")
                except Exception as e:
                    logger.error(f"Error pausing {pid}: {e}")

    async def _resume_all_paused_by_disconnect(self) -> None:
        """Resume strategies that were auto-paused by disconnect."""
        if not self._mktdata_connected or not self._om_connected:
            return
        for pid, strategy in self._strategies.items():
            if strategy._paused and strategy.parent_order:
                if strategy.parent_order.state == ParentState.PAUSED:
                    try:
                        await strategy.resume()
                        logger.info(f"Auto-resumed {pid} after reconnect")
                    except Exception as e:
                        logger.error(f"Error resuming {pid}: {e}")
