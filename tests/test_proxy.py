"""Tests for proxy.py -- WS routing, HTTP forwarding, error handling."""

import asyncio
import json
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from proxy import WS_ROUTES, create_app, GUI_ORIGIN


# ---------------------------------------------------------------------------
# Route table
# ---------------------------------------------------------------------------

class TestRouteTable:
    def test_mktdata_route(self):
        assert WS_ROUTES["/ws/mktdata"] == "ws://localhost:8081"

    def test_guibroker_route(self):
        assert WS_ROUTES["/ws/guibroker"] == "ws://localhost:8082"

    def test_posmanager_route(self):
        assert WS_ROUTES["/ws/posmanager"] == "ws://localhost:8085"

    def test_route_count(self):
        assert len(WS_ROUTES) == 3

    def test_no_unknown_routes(self):
        for path in WS_ROUTES:
            assert path.startswith("/ws/")


# ---------------------------------------------------------------------------
# HTTP proxy (using aiohttp test client)
# ---------------------------------------------------------------------------

@pytest.fixture
def app():
    return create_app()


@pytest.mark.asyncio
async def test_http_proxy_get(aiohttp_client, app):
    """GET / should proxy to the GUI backend; mock the backend response."""
    import aiohttp

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.headers = {"Content-Type": "text/html"}
    mock_resp.read = AsyncMock(return_value=b"<html>hello</html>")

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_session_instance = AsyncMock()
    mock_session_instance.request = MagicMock(return_value=mock_ctx)
    mock_session_instance.__aenter__ = AsyncMock(return_value=mock_session_instance)
    mock_session_instance.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session_instance):
        client = await aiohttp_client(app)
        resp = await client.get("/")
        assert resp.status == 200
        body = await resp.read()
        assert body == b"<html>hello</html>"


@pytest.mark.asyncio
async def test_http_proxy_post(aiohttp_client, app):
    """POST /api/risk-limits should forward body to backend."""
    import aiohttp

    payload = {"max_order_size": 100}
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.read = AsyncMock(return_value=json.dumps({"status": "ok"}).encode())

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_session_instance = AsyncMock()
    mock_session_instance.request = MagicMock(return_value=mock_ctx)
    mock_session_instance.__aenter__ = AsyncMock(return_value=mock_session_instance)
    mock_session_instance.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session_instance):
        client = await aiohttp_client(app)
        resp = await client.post("/api/risk-limits", json=payload)
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_http_proxy_backend_unavailable(aiohttp_client, app):
    """When backend is down, proxy should return 502."""
    import aiohttp

    mock_session_instance = AsyncMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("connection refused"))
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_session_instance.request = MagicMock(return_value=mock_ctx)
    mock_session_instance.__aenter__ = AsyncMock(return_value=mock_session_instance)
    mock_session_instance.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session_instance):
        client = await aiohttp_client(app)
        resp = await client.get("/api/status")
        assert resp.status == 502
        text = await resp.text()
        assert "Backend unavailable" in text


# ---------------------------------------------------------------------------
# SSE streaming
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_http_proxy_sse_streaming(aiohttp_client, app):
    """SSE responses (text/event-stream) should be streamed through."""
    import aiohttp

    sse_chunks = [b"data: {\"text\":\"hello\"}\n\n", b"data: {\"done\":true}\n\n"]

    mock_content = AsyncMock()

    async def _iter_any():
        for chunk in sse_chunks:
            yield chunk

    mock_content.iter_any = _iter_any

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.headers = {"Content-Type": "text/event-stream"}
    mock_resp.content = mock_content

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_session_instance = AsyncMock()
    mock_session_instance.request = MagicMock(return_value=mock_ctx)
    mock_session_instance.__aenter__ = AsyncMock(return_value=mock_session_instance)
    mock_session_instance.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session_instance):
        client = await aiohttp_client(app)
        resp = await client.post("/api/troubleshoot", json={"question": "test"})
        assert resp.status == 200
        body = await resp.read()
        assert b"hello" in body
        assert b"done" in body


# ---------------------------------------------------------------------------
# App creation
# ---------------------------------------------------------------------------

class TestAppCreation:
    def test_create_app_returns_application(self):
        from aiohttp.web import Application
        app = create_app()
        assert isinstance(app, Application)

    def test_create_app_has_catch_all_route(self):
        app = create_app()
        # The app should have at least one route (the catch-all)
        assert len(app.router.routes()) > 0
