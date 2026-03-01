"""
Simple HTTP server to serve the GUI static files on port 8080.
Includes /api/config endpoint for system configuration.
"""
import collections
import http.server
import json
import logging
import socket
import socketserver
import os
import sys

PORT = 8080
DIRECTORY = os.path.dirname(os.path.abspath(__file__))

# Import config (add parent dir to path if needed)
sys.path.insert(0, os.path.dirname(DIRECTORY))
from shared.config import (
    USE_REAL_COINBASE, COINBASE_MODE, COINBASE_API_KEY_NAME,
    COINBASE_REST_URL, COINBASE_WS_MARKET_URL, COINBASE_WS_USER_URL,
    USE_COINBASE_FIX, CB_FIX_API_KEY, COINBASE_FIX_ORD_HOST, COINBASE_FIX_MD_HOST, COINBASE_FIX_PORT,
    PORTS, HOST, SYMBOLS, EXCHANGES, DEFAULT_ROUTING,
    ANTHROPIC_API_KEY, ARCHITECTURE_FILE,
)
from shared.logging_config import setup_component_logging
from shared import message_store
from shared import risk_limits

logger = setup_component_logging("GUI")

# Load ARCHITECTURE.md content at startup
_architecture_content = ""
try:
    with open(ARCHITECTURE_FILE, "r", encoding="utf-8") as f:
        _architecture_content = f.read()
except OSError as e:
    logger.warning(f"Could not load architecture file: {e}")

_LOGS_DIR = os.path.join(os.path.dirname(DIRECTORY), "logs")


def _read_recent_messages(limit=200):
    """Return a formatted table of recent inter-component messages from the DB."""
    try:
        rows = message_store.query_recent(limit)
    except Exception:
        return "(message database not available)\n"
    if not rows:
        return "(no messages recorded yet)\n"
    # Rows come newest-first; reverse so the table reads chronologically
    rows.reverse()
    lines = ["TIMESTAMP            | COMPONENT   | DIR  | PEER        | DESCRIPTION"]
    lines.append("---------------------|-------------|------|-------------|" + "-" * 50)
    for r in rows:
        ts = r["timestamp"][11:23] if len(r["timestamp"]) >= 23 else r["timestamp"]
        comp = r["component"].ljust(11)
        d = r["direction"].ljust(4)
        peer = r["peer"].ljust(11)
        lines.append(f"{ts} | {comp} | {d} | {peer} | {r['description']}")
    return "\n".join(lines) + "\n"


def _read_recent_logs(tail_lines=50):
    """Return the last *tail_lines* from each current .log file in logs/."""
    sections = []
    try:
        files = sorted(
            f for f in os.listdir(_LOGS_DIR)
            if f.endswith(".log") and not any(f.endswith(f".log.{i}") for i in range(1, 10))
        )
    except OSError:
        return "(logs directory not found)\n"

    for fname in files:
        path = os.path.join(_LOGS_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = collections.deque(fh, maxlen=tail_lines)
            sections.append(f"--- {fname} (last {tail_lines} lines) ---\n" + "".join(lines))
        except OSError:
            sections.append(f"--- {fname} ---\n(could not read)\n")
    return "\n".join(sections) if sections else "(no log files found)\n"


def _build_config_response():
    """Build the system configuration summary as a dict."""
    # Determine Coinbase adapter descriptions
    if USE_COINBASE_FIX:
        cb_md = "CoinbaseFIXFeed (FIX 5.0)"
        cb_ex = "CoinbaseFIXAdapter (FIX 5.0)"
    elif USE_REAL_COINBASE:
        cb_md = "CoinbaseLiveFeed (REST/WS)"
        cb_ex = "CoinbaseAdapter (REST)"
    else:
        cb_md = "CoinbaseFeed (simulator)"
        cb_ex = "CoinbaseSimulator"

    # Determine connectivity mode label
    if USE_COINBASE_FIX:
        mode = "FIX"
    elif USE_REAL_COINBASE:
        mode = "REAL"
    else:
        mode = "SIMULATOR"

    resp = {
        "system": {
            "mode": mode,
            "host": HOST,
        },
        "coinbase": {
            "mode": COINBASE_MODE,
            "api_key_configured": bool(COINBASE_API_KEY_NAME),
            "api_key_name": COINBASE_API_KEY_NAME[:20] + "..." if len(COINBASE_API_KEY_NAME) > 20 else COINBASE_API_KEY_NAME or "(not set)",
            "rest_url": COINBASE_REST_URL.get(COINBASE_MODE, ""),
            "ws_market_url": COINBASE_WS_MARKET_URL,
            "ws_user_url": COINBASE_WS_USER_URL,
        },
        "coinbase_fix": {
            "enabled": USE_COINBASE_FIX,
            "api_key_configured": bool(CB_FIX_API_KEY),
            "api_key_name": CB_FIX_API_KEY or "(not set)",
            "ord_host": COINBASE_FIX_ORD_HOST.get(COINBASE_MODE, ""),
            "md_host": COINBASE_FIX_MD_HOST.get(COINBASE_MODE, ""),
            "port": COINBASE_FIX_PORT,
        },
        "components": {
            name: {"port": port, "url": f"ws://{HOST}:{port}"}
            for name, port in PORTS.items()
        },
        "symbols": SYMBOLS,
        "exchanges": {
            name: {"name": cfg["name"], "symbols": cfg["symbols"]}
            for name, cfg in EXCHANGES.items()
        },
        "routing": DEFAULT_ROUTING,
        "adapters": {
            "BINANCE": {
                "market_data": "BinanceFeed (simulator)",
                "exchange": "BinanceSimulator",
            },
            "COINBASE": {
                "market_data": cb_md,
                "exchange": cb_ex,
            },
        },
    }
    return resp


def _probe_port(port, host=HOST, timeout=0.5):
    """Return True if a TCP connection to host:port succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


_FIX_STATUS_FILE = os.path.join(os.path.dirname(DIRECTORY), "logs", "fix_status.json")


def _read_fix_status():
    """Read FIX connection status from the shared status file."""
    try:
        with open(_FIX_STATUS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _build_status_response():
    """Probe each component port and return availability + exchange mode info."""
    components = {name: _probe_port(port) for name, port in PORTS.items()}

    if USE_COINBASE_FIX:
        cb_mode = ("FIX_PRODUCTION" if COINBASE_MODE == "production" else "FIX_SANDBOX")
    elif USE_REAL_COINBASE:
        cb_mode = ("PRODUCTION" if COINBASE_MODE == "production" else "SANDBOX")
    else:
        cb_mode = "SIMULATOR"

    exchanges = {
        "BINANCE": {"mode": "SIMULATOR"},
        "COINBASE": {"mode": cb_mode},
    }

    # Include FIX session status when FIX is enabled
    fix_status = None
    if USE_COINBASE_FIX:
        raw = _read_fix_status()
        fix_status = {}
        for name, info in raw.items():
            fix_status[name] = {
                "logged_in": info.get("logged_in", False),
                "connected": info.get("connected", False),
                "host": info.get("host", ""),
                "last_logout_reason": info.get("last_logout_reason", ""),
            }

        # If FIX sessions aren't logged in, mark the dependent components as down
        ord_up = fix_status.get("FIX-ORD", {}).get("logged_in", False)
        md_up = fix_status.get("FIX-MD", {}).get("logged_in", False)
        if not ord_up:
            components["EXCHCONN"] = False
        if not md_up:
            components["MKTDATA"] = False

    resp = {"components": components, "exchanges": exchanges}
    if fix_status is not None:
        resp["fix_sessions"] = fix_status
    return resp


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def do_GET(self):
        if self.path == "/api/config":
            data = json.dumps(_build_config_response())
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data.encode("utf-8"))
            return
        if self.path == "/api/status":
            data = json.dumps(_build_status_response())
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data.encode("utf-8"))
            return
        if self.path == "/api/risk-limits":
            data = json.dumps(risk_limits.load_limits())
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data.encode("utf-8"))
            return
        if self.path.startswith("/api/records"):
            try:
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(self.path).query)
                limit = min(int(qs.get("limit", ["200"])[0]), 1000)
                rows = message_store.query_recent(limit)
                data = json.dumps(rows)
            except Exception as e:
                data = json.dumps({"error": str(e)})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data.encode("utf-8"))
            return
        return super().do_GET()

    def do_POST(self):
        if self.path == "/api/risk-limits":
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length == 0:
                    resp = json.dumps({"status": "error", "message": "Empty request body"})
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(resp.encode("utf-8"))
                    return
                body = self.rfile.read(length)
                limits = json.loads(body)
                risk_limits.save_limits(limits)
                resp = json.dumps({"status": "ok"})
                self.send_response(200)
            except (json.JSONDecodeError, ValueError) as e:
                resp = json.dumps({"status": "error", "message": str(e)})
                self.send_response(400)
            except OSError as e:
                resp = json.dumps({"status": "error", "message": str(e)})
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(resp.encode("utf-8"))
            return

        if self.path == "/api/troubleshoot":
            if not ANTHROPIC_API_KEY:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "ANTHROPIC_API_KEY not configured"}).encode("utf-8"))
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                question = body.get("question", "").strip()
                if not question:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "No question provided"}).encode("utf-8"))
                    return

                import anthropic
                client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

                system_prompt = (
                    "You are a troubleshooting assistant for a crypto trading system. "
                    "Use the architecture documentation, inter-component message records, and recent log output below to answer questions accurately.\n\n"
                    "--- ARCHITECTURE ---\n" + _architecture_content
                    + "\n\n--- RECENT MESSAGES ---\n" + _read_recent_messages()
                    + "\n\n--- RECENT LOGS ---\n" + _read_recent_logs()
                )

                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

                with client.messages.stream(
                    model="claude-sonnet-4-20250514",
                    max_tokens=2048,
                    system=system_prompt,
                    messages=[{"role": "user", "content": question}],
                ) as stream:
                    for text in stream.text_stream:
                        chunk = json.dumps({"text": text})
                        self.wfile.write(f"data: {chunk}\n\n".encode("utf-8"))
                        self.wfile.flush()

                self.wfile.write(b"data: {\"done\": true}\n\n")
                self.wfile.flush()
            except Exception as e:
                logger.error(f"Troubleshoot error: {e}")
                try:
                    err = json.dumps({"error": str(e)})
                    self.wfile.write(f"event: error\ndata: {err}\n\n".encode("utf-8"))
                    self.wfile.write(b"data: {\"done\": true}\n\n")
                    self.wfile.flush()
                except Exception as e2:
                    logger.warning(f"Failed to send error response to client: {e2}")
            return

    def log_message(self, format, *args):
        logger.info(f"[HTTP] {args[0]}")


def main():
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        logger.info(f"Serving on http://localhost:{PORT}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            logger.info("Shutting down.")
            httpd.shutdown()


if __name__ == "__main__":
    main()
