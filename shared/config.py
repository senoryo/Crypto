"""
Shared configuration for the Crypto Trading System.
All component ports, addresses, symbols, and exchange definitions.
"""

import os
from dotenv import load_dotenv

load_dotenv(override=True)

# --- Risk limits configuration ---
RISK_LIMITS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "risk_limits.json")

# --- Anthropic API configuration ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ARCHITECTURE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ARCHITECTURE.md")
MESSAGE_DB_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "messages.db")

DEFAULT_RISK_LIMITS = {
    "max_order_qty": {
        "BTC/USD": 10.0,
        "ETH/USD": 100.0,
        "SOL/USD": 5000.0,
        "ADA/USD": 100000.0,
        "DOGE/USD": 500000.0,
    },
    "max_order_notional": 100000.0,
    "max_position_qty": {
        "BTC/USD": 50.0,
        "ETH/USD": 500.0,
        "SOL/USD": 25000.0,
        "ADA/USD": 500000.0,
        "DOGE/USD": 2500000.0,
    },
    "max_open_orders": 50,
}

# --- Coinbase API configuration ---
USE_REAL_COINBASE = os.getenv("USE_REAL_COINBASE", "false").lower() == "true"
COINBASE_MODE = os.getenv("COINBASE_MODE", "sandbox")  # "sandbox" or "production"
COINBASE_API_KEY_NAME = os.getenv("COINBASE_API_KEY_NAME", "")
COINBASE_API_SECRET = os.getenv("COINBASE_API_SECRET", "").replace("\\n", "\n")

# --- Coinbase Exchange FIX API configuration ---
USE_COINBASE_FIX = os.getenv("USE_COINBASE_FIX", "false").lower() == "true"
CB_FIX_API_KEY = os.getenv("CB_FIX_API_KEY", "")
CB_FIX_PASSPHRASE = os.getenv("CB_FIX_PASSPHRASE", "")
CB_FIX_SECRET = os.getenv("CB_FIX_SECRET", "")

COINBASE_FIX_ORD_HOST = {
    "sandbox": "fix-ord.sandbox.exchange.coinbase.com",
    "production": "fix-ord.exchange.coinbase.com",
}
COINBASE_FIX_MD_HOST = {
    "sandbox": "fix-md.sandbox.exchange.coinbase.com",
    "production": "fix-md.exchange.coinbase.com",
}
COINBASE_FIX_PORT = 6121

COINBASE_REST_URL = {
    "sandbox": "https://api-sandbox.coinbase.com",
    "production": "https://api.coinbase.com",
}

COINBASE_WS_MARKET_URL = "wss://advanced-trade-ws.coinbase.com"
COINBASE_WS_USER_URL = "wss://advanced-trade-ws-user.coinbase.com"

# WebSocket ports for each component
PORTS = {
    "GUI_HTTP": 8080,
    "MKTDATA": 8081,
    "GUIBROKER": 8082,
    "OM": 8083,
    "EXCHCONN": 8084,
    "POSMANAGER": 8085,
    "ALGO": 8086,
}

HOST = "localhost"

# Supported crypto symbols
SYMBOLS = [
    "BTC/USD",
    "ETH/USD",
    "SOL/USD",
    "ADA/USD",
    "DOGE/USD",
]

# Supported exchanges and their symbol mappings
EXCHANGES = {
    "BINANCE": {
        "name": "Binance",
        "symbols": {
            "BTC/USD": "BTCUSDT",
            "ETH/USD": "ETHUSDT",
            "SOL/USD": "SOLUSDT",
            "ADA/USD": "ADAUSDT",
            "DOGE/USD": "DOGEUSDT",
        },
    },
    "COINBASE": {
        "name": "Coinbase",
        "symbols": {
            "BTC/USD": "BTC-USD",
            "ETH/USD": "ETH-USD",
            "SOL/USD": "SOL-USD",
            "ADA/USD": "ADA-USD",
            "DOGE/USD": "DOGE-USD",
        },
    },
    "KRAKEN": {
        "name": "Kraken",
        "symbols": {
            "BTC/USD": "XXBTZUSD",
            "ETH/USD": "XETHZUSD",
            "SOL/USD": "SOLUSD",
            "ADA/USD": "ADAUSD",
            "DOGE/USD": "XDGUSD",
        },
    },
    "BYBIT": {
        "name": "Bybit",
        "symbols": {
            "BTC/USD": "BTCUSDT",
            "ETH/USD": "ETHUSDT",
            "SOL/USD": "SOLUSDT",
            "ADA/USD": "ADAUSDT",
            "DOGE/USD": "DOGEUSDT",
        },
    },
    "OKX": {
        "name": "OKX",
        "symbols": {
            "BTC/USD": "BTC-USDT",
            "ETH/USD": "ETH-USDT",
            "SOL/USD": "SOL-USDT",
            "ADA/USD": "ADA-USDT",
            "DOGE/USD": "DOGE-USDT",
        },
    },
    "BITFINEX": {
        "name": "Bitfinex",
        "symbols": {
            "BTC/USD": "tBTCUSD",
            "ETH/USD": "tETHUSD",
            "SOL/USD": "tSOLUSD",
            "ADA/USD": "tADAUSD",
            "DOGE/USD": "tDOGEUSD",
        },
    },
    "HTX": {
        "name": "HTX",
        "symbols": {
            "BTC/USD": "btcusdt",
            "ETH/USD": "ethusdt",
            "SOL/USD": "solusdt",
            "ADA/USD": "adausdt",
            "DOGE/USD": "dogeusdt",
        },
    },
}

# Default exchange routing: symbol -> preferred exchange
DEFAULT_ROUTING = {
    "BTC/USD": "BINANCE",
    "ETH/USD": "KRAKEN",
    "SOL/USD": "COINBASE",
    "ADA/USD": "OKX",
    "DOGE/USD": "BYBIT",
}

# Order sides
SIDE_BUY = "1"
SIDE_SELL = "2"

# Order types
ORD_TYPE_MARKET = "1"
ORD_TYPE_LIMIT = "2"

# WebSocket URLs for component connections
def ws_url(component: str) -> str:
    return f"ws://{HOST}:{PORTS[component]}"
