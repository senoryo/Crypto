"""Tests for gui/server.py -- HTTP handler, API endpoints, static file serving."""

import io
import json
from unittest.mock import patch, MagicMock, PropertyMock

import pytest


# ---------------------------------------------------------------------------
# Helper to build a mock Handler without triggering __init__ (which tries to
# serve files and write to a real socket).
# ---------------------------------------------------------------------------

def _make_handler(path="/", method="GET", body=b""):
    """Return a Handler instance with mocked internals for the given request."""
    from gui.server import Handler

    handler = object.__new__(Handler)
    handler.path = path
    handler.command = method
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.client_address = ("127.0.0.1", 55555)
    handler.request_version = "HTTP/1.1"

    # Capture response metadata
    handler._response_code = None
    handler._response_headers = {}

    def mock_send_response(code, message=None):
        handler._response_code = code

    def mock_send_header(key, value):
        handler._response_headers[key] = value

    def mock_end_headers():
        pass

    handler.send_response = mock_send_response
    handler.send_header = mock_send_header
    handler.end_headers = mock_end_headers

    return handler


def _get_response_json(handler):
    """Read the JSON body written to handler.wfile."""
    handler.wfile.seek(0)
    return json.loads(handler.wfile.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Tests for /api/config
# ---------------------------------------------------------------------------

class TestConfigEndpoint:

    def test_returns_200(self):
        handler = _make_handler("/api/config")
        handler.do_GET()
        assert handler._response_code == 200

    def test_returns_json_content_type(self):
        handler = _make_handler("/api/config")
        handler.do_GET()
        assert handler._response_headers.get("Content-Type") == "application/json"

    def test_has_cors_header(self):
        handler = _make_handler("/api/config")
        handler.do_GET()
        assert handler._response_headers.get("Access-Control-Allow-Origin") == "*"

    def test_response_has_expected_top_level_keys(self):
        handler = _make_handler("/api/config")
        handler.do_GET()
        data = _get_response_json(handler)
        expected_keys = {"system", "coinbase", "coinbase_fix", "components",
                         "symbols", "exchanges", "routing", "adapters"}
        assert expected_keys.issubset(data.keys())

    def test_system_mode_is_simulator_by_default(self):
        handler = _make_handler("/api/config")
        with patch("gui.server.USE_COINBASE_FIX", False), \
             patch("gui.server.USE_REAL_COINBASE", False):
            handler.do_GET()
        data = _get_response_json(handler)
        assert data["system"]["mode"] == "SIMULATOR"

    def test_symbols_list_present(self):
        handler = _make_handler("/api/config")
        handler.do_GET()
        data = _get_response_json(handler)
        assert isinstance(data["symbols"], list)
        assert len(data["symbols"]) > 0


# ---------------------------------------------------------------------------
# Tests for /api/status
# ---------------------------------------------------------------------------

class TestStatusEndpoint:

    @patch("gui.server._probe_port", return_value=False)
    def test_returns_200(self, mock_probe):
        handler = _make_handler("/api/status")
        handler.do_GET()
        assert handler._response_code == 200

    @patch("gui.server._probe_port", return_value=False)
    def test_response_has_components_and_exchanges(self, mock_probe):
        handler = _make_handler("/api/status")
        handler.do_GET()
        data = _get_response_json(handler)
        assert "components" in data
        assert "exchanges" in data


# ---------------------------------------------------------------------------
# Tests for /api/risk-limits
# ---------------------------------------------------------------------------

class TestRiskLimitsEndpoint:

    @patch("gui.server.risk_limits")
    def test_get_risk_limits_returns_200(self, mock_rl):
        mock_rl.load_limits.return_value = {"max_order_qty": 100}
        handler = _make_handler("/api/risk-limits")
        handler.do_GET()
        assert handler._response_code == 200
        data = _get_response_json(handler)
        assert data == {"max_order_qty": 100}

    @patch("gui.server.risk_limits")
    def test_post_risk_limits_saves(self, mock_rl):
        new_limits = {"max_order_qty": 50}
        body = json.dumps(new_limits).encode("utf-8")
        handler = _make_handler("/api/risk-limits", method="POST", body=body)
        handler.do_POST()
        assert handler._response_code == 200
        mock_rl.save_limits.assert_called_once_with(new_limits)
        data = _get_response_json(handler)
        assert data["status"] == "ok"

    @patch("gui.server.risk_limits")
    def test_post_invalid_json_returns_400(self, mock_rl):
        body = b"not valid json"
        handler = _make_handler("/api/risk-limits", method="POST", body=body)
        handler.do_POST()
        assert handler._response_code == 400
        data = _get_response_json(handler)
        assert data["status"] == "error"


# ---------------------------------------------------------------------------
# Tests for static file serving fallthrough
# ---------------------------------------------------------------------------

class TestStaticFileServing:

    def test_non_api_path_falls_through_to_super(self):
        """Non-API paths should delegate to SimpleHTTPRequestHandler.do_GET."""
        handler = _make_handler("/index.html")
        with patch("http.server.SimpleHTTPRequestHandler.do_GET") as mock_super_get:
            handler.do_GET()
            mock_super_get.assert_called_once()
