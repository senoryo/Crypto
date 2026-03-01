"""
WebSocket transport layer for inter-component communication.

Provides:
- WSServer: async WebSocket server for components that accept connections
- WSClient: async WebSocket client for connecting to other components
- PubSub: simple publish/subscribe for broadcasting messages
"""

import asyncio
import json
import logging
from typing import Callable, Optional, Set

import websockets
from websockets.asyncio.server import Server, ServerConnection
from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)


class WSServer:
    """WebSocket server that manages connected clients and message handling."""

    def __init__(self, host: str, port: int, name: str = "WSServer"):
        self.host = host
        self.port = port
        self.name = name
        self.clients: Set[ServerConnection] = set()
        self._server: Optional[Server] = None
        self._on_message: Optional[Callable] = None
        self._on_connect: Optional[Callable] = None
        self._on_disconnect: Optional[Callable] = None

    def on_message(self, handler: Callable):
        """Register a message handler: async def handler(websocket, message_str)"""
        self._on_message = handler

    def on_connect(self, handler: Callable):
        """Register a connect handler: async def handler(websocket)"""
        self._on_connect = handler

    def on_disconnect(self, handler: Callable):
        """Register a disconnect handler: async def handler(websocket)"""
        self._on_disconnect = handler

    async def _handler(self, websocket: ServerConnection):
        self.clients.add(websocket)
        remote = websocket.remote_address
        logger.info(f"[{self.name}] Client connected: {remote}")
        if self._on_connect:
            await self._on_connect(websocket)
        try:
            async for message in websocket:
                if self._on_message:
                    await self._on_message(websocket, message)
        except websockets.ConnectionClosed:
            logger.info(f"[{self.name}] Client disconnected: {remote}")
        finally:
            self.clients.discard(websocket)
            if self._on_disconnect:
                await self._on_disconnect(websocket)

    async def broadcast(self, message: str, exclude: Optional[ServerConnection] = None):
        """Send a message to all connected clients."""
        targets = self.clients - {exclude} if exclude else self.clients
        if targets:
            results = await asyncio.gather(
                *[client.send(message) for client in targets],
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, Exception):
                    logger.debug(f"[{self.name}] Broadcast send failed: {result}")

    async def send_to(self, websocket: ServerConnection, message: str):
        """Send a message to a specific client."""
        try:
            await websocket.send(message)
        except websockets.ConnectionClosed:
            self.clients.discard(websocket)

    async def start(self):
        """Start the WebSocket server."""
        self._server = await websockets.serve(
            self._handler, self.host, self.port
        )
        logger.info(f"[{self.name}] Listening on ws://{self.host}:{self.port}")

    async def stop(self):
        """Stop the WebSocket server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info(f"[{self.name}] Stopped")


class WSClient:
    """WebSocket client for connecting to other components."""

    def __init__(self, url: str, name: str = "WSClient"):
        self.url = url
        self.name = name
        self._ws: Optional[ClientConnection] = None
        self._on_message: Optional[Callable] = None
        self._running = False
        self._reconnect_delay = 2.0

    @property
    def is_connected(self) -> bool:
        """Whether the client has an active WebSocket connection."""
        return self._ws is not None

    def on_message(self, handler: Callable):
        """Register a message handler: async def handler(message_str)"""
        self._on_message = handler

    async def connect(self, retry: bool = True):
        """Connect to the WebSocket server with optional retry."""
        self._running = True
        while self._running:
            try:
                self._ws = await websockets.connect(self.url)
                logger.info(f"[{self.name}] Connected to {self.url}")
                return
            except (ConnectionRefusedError, OSError) as e:
                if not retry:
                    raise
                logger.warning(
                    f"[{self.name}] Connection to {self.url} failed: {e}. "
                    f"Retrying in {self._reconnect_delay}s..."
                )
                await asyncio.sleep(self._reconnect_delay)

    async def send(self, message: str):
        """Send a message to the connected server."""
        if self._ws:
            await self._ws.send(message)

    async def listen(self):
        """Listen for messages from the server. Reconnects on disconnect."""
        while self._running:
            try:
                if not self._ws:
                    await self.connect()
                async for message in self._ws:
                    if self._on_message:
                        await self._on_message(message)
            except websockets.ConnectionClosed:
                logger.warning(f"[{self.name}] Disconnected from {self.url}. Reconnecting...")
                if self._ws:
                    try:
                        await self._ws.close()
                    except Exception:
                        pass
                self._ws = None
                await asyncio.sleep(self._reconnect_delay)
            except Exception as e:
                logger.error(f"[{self.name}] Error: {e}")
                if self._ws:
                    try:
                        await self._ws.close()
                    except Exception:
                        pass
                self._ws = None
                await asyncio.sleep(self._reconnect_delay)

    async def close(self):
        """Close the connection."""
        self._running = False
        if self._ws:
            await self._ws.close()


class PubSub:
    """Simple in-process publish/subscribe for message routing."""

    def __init__(self):
        self._subscribers: dict[str, list[Callable]] = {}

    def subscribe(self, topic: str, handler: Callable):
        """Subscribe to a topic."""
        if topic not in self._subscribers:
            self._subscribers[topic] = []
        self._subscribers[topic].append(handler)

    def unsubscribe(self, topic: str, handler: Callable):
        """Unsubscribe from a topic."""
        if topic in self._subscribers:
            try:
                self._subscribers[topic].remove(handler)
            except ValueError:
                pass

    async def publish(self, topic: str, message):
        """Publish a message to all subscribers of a topic."""
        handlers = self._subscribers.get(topic, [])
        for handler in handlers:
            try:
                await handler(message)
            except Exception as e:
                logger.error(f"PubSub handler error on topic '{topic}': {e}")


def json_msg(msg_type: str, **kwargs) -> str:
    """Create a JSON message string for non-FIX communication (e.g., GUI updates)."""
    return json.dumps({"type": msg_type, **kwargs})


def parse_json_msg(raw: str) -> dict:
    """Parse a JSON message string."""
    return json.loads(raw)
