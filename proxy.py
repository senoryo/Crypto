"""
Reverse proxy for single-port deployment (e.g. Render).

Multiplexes browser connections through one port:
  /ws/mktdata     -> ws://localhost:8081
  /ws/guibroker   -> ws://localhost:8082
  /ws/posmanager  -> ws://localhost:8085
  /*              -> http://localhost:8080  (GUI server)
"""

import asyncio
import logging
import os

import aiohttp
from aiohttp import web

logger = logging.getLogger("PROXY")

PORT = int(os.environ.get("PORT", 10000))
GUI_ORIGIN = "http://localhost:8080"

WS_ROUTES = {
    "/ws/mktdata": "ws://localhost:8081",
    "/ws/guibroker": "ws://localhost:8082",
    "/ws/posmanager": "ws://localhost:8085",
}


async def _ws_proxy(request: web.Request) -> web.WebSocketResponse:
    """Proxy a browser WebSocket to an internal backend WebSocket."""
    backend_url = WS_ROUTES.get(request.path)
    if backend_url is None:
        raise web.HTTPNotFound(text=f"No WS backend for {request.path}")

    ws_resp = web.WebSocketResponse(heartbeat=30)
    await ws_resp.prepare(request)

    session = aiohttp.ClientSession()
    try:
        backend_ws = await session.ws_connect(backend_url, heartbeat=30)
    except Exception as exc:
        logger.error("Cannot connect to backend %s: %s", backend_url, exc)
        await session.close()
        await ws_resp.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b"backend unavailable")
        return ws_resp

    async def _browser_to_backend():
        async for msg in ws_resp:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await backend_ws.send_str(msg.data)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                await backend_ws.send_bytes(msg.data)
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                break

    async def _backend_to_browser():
        async for msg in backend_ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await ws_resp.send_str(msg.data)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                await ws_resp.send_bytes(msg.data)
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                break

    try:
        tasks = [
            asyncio.create_task(_browser_to_backend()),
            asyncio.create_task(_backend_to_browser()),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for task in done:
            if task.exception():
                logger.warning("WS proxy task error: %s", task.exception())
    except Exception as exc:
        logger.warning("WS proxy error: %s", exc)
    finally:
        await backend_ws.close()
        await session.close()

    return ws_resp


async def _http_proxy(request: web.Request) -> web.StreamResponse:
    """Forward HTTP requests to the internal GUI server."""
    target_url = GUI_ORIGIN + request.path_qs

    body = await request.read()
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "transfer-encoding")
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.request(
                request.method, target_url,
                headers=headers, data=body,
                allow_redirects=False,
            ) as backend_resp:
                content_type = backend_resp.headers.get("Content-Type", "")

                # Stream SSE responses (e.g. /api/troubleshoot)
                if "text/event-stream" in content_type:
                    resp = web.StreamResponse(
                        status=backend_resp.status,
                        headers={
                            "Content-Type": "text/event-stream",
                            "Cache-Control": "no-cache",
                            "Access-Control-Allow-Origin": "*",
                        },
                    )
                    await resp.prepare(request)
                    async for chunk in backend_resp.content.iter_any():
                        await resp.write(chunk)
                    await resp.write_eof()
                    return resp

                # Full response for everything else
                resp_body = await backend_resp.read()
                resp = web.Response(
                    status=backend_resp.status,
                    body=resp_body,
                    content_type=content_type or "application/octet-stream",
                )
                # Copy relevant headers
                for hdr in ("Access-Control-Allow-Origin", "Cache-Control", "Location"):
                    val = backend_resp.headers.get(hdr)
                    if val:
                        resp.headers[hdr] = val
                return resp

        except aiohttp.ClientError as exc:
            logger.error("Backend request failed: %s", exc)
            return web.Response(status=502, text=f"Backend unavailable: {exc}")


async def _handler(request: web.Request):
    """Route to WS proxy or HTTP proxy."""
    if request.path in WS_ROUTES:
        return await _ws_proxy(request)
    return await _http_proxy(request)


def create_app() -> web.Application:
    """Build the aiohttp application."""
    app = web.Application()
    app.router.add_route("*", "/{path_info:.*}", _handler)
    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    logger.info("Proxy listening on 0.0.0.0:%d", PORT)
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=PORT, print=None)


if __name__ == "__main__":
    main()
