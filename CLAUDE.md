# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Distributed, async crypto trading platform built in Python. Six independent components communicate over WebSocket using FIX protocol (order flow) and JSON (market data/positions). Supports simulated exchanges and real Coinbase connectivity.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Start all components (opens browser to http://localhost:8080)
python run_all.py

# Start backend only (no browser)
python run_all.py --no-gui

# Kill all running components and restart
python restart.py
python restart.py --no-gui

# Run a single component
python -m mktdata.mktdata      # port 8081
python -m exchconn.exchconn    # port 8084
python -m posmanager.posmanager # port 8085
python -m om.order_manager     # port 8083
python -m guibroker.guibroker  # port 8082
python -m gui.server           # port 8080
```

Components must start in dependency order: MKTDATA & EXCHCONN first, then POSMANAGER, OM, GUIBROKER, GUI last. Test suite: `pytest -v` (156 tests across 11 files in tests/).

## Architecture

```
GUI ──JSON──> GUIBROKER ──FIX──> OM ──FIX──> EXCHCONN ──REST/WS──> Exchanges
 ^                                |                                  (Binance, Coinbase)
 |                                | fills (JSON)
 |                                v
 +──JSON──── POSMANAGER <──JSON── MKTDATA <── Exchange Feeds
 +──JSON──── MKTDATA
```

**Six components, each on its own port (8080-8085):**

- **GUI** (8080) — Python `http.server` serving a single-page trading terminal (`gui/index.html`, `gui/app.js`, `gui/styles.css`). REST endpoints at `/api/config`, `/api/status`, `/api/risk-limits`.
- **MKTDATA** (8081) — Aggregates exchange feeds (simulated or real), broadcasts JSON market data ticks to subscribers (GUI, POSMANAGER).
- **GUIBROKER** (8082) — Protocol bridge: translates JSON from GUI to FIX for OM, and FIX execution reports back to JSON. Assigns `ClOrdID` (GUI-1, GUI-2, ...), maintains cancel/amend ID mappings.
- **OM** (8083) — Central order router. Validates orders, applies risk checks (from `risk_limits.json`), assigns OM IDs (`OM-000001`), routes to EXCHCONN, tracks positions internally for risk, forwards execution reports back.
- **EXCHCONN** (8084) — Routes FIX orders to the appropriate exchange simulator (BinanceSimulator or CoinbaseSimulator) or real CoinbaseAdapter. Uses `ExDestination` tag or default routing table.
- **POSMANAGER** (8085) — Tracks positions and P&L. Receives fills from OM (JSON), market prices from MKTDATA. Broadcasts position updates to GUI (throttled 2/sec).

## Shared Modules (`shared/`)

- `config.py` — Ports, symbols, exchanges, routing defaults, risk limit defaults
- `fix_protocol.py` — FIX 4.4 message implementation (`FIXMessage` class, factory functions)
- `fix_engine.py` — FIX engine for protocol handling
- `ws_transport.py` — `WSServer` (async server with broadcast), `WSClient` (auto-reconnecting client)
- `risk_limits.py` — Risk limit persistence and checking (`load_limits()`, `save_limits()`, `check_order()`)
- `logging_config.py` — Per-component logging with `log_recv`/`log_send` helpers
- `coinbase_auth.py` — Coinbase authentication utilities
- `message_store.py` — Message storage/retrieval

## Key Conventions

- FIX protocol for inter-component order flow (OM ↔ EXCHCONN, GUIBROKER ↔ OM); JSON for everything else
- Each component runs as `python -m <package>.<module>` from the project root
- Logs go to `logs/<COMPONENT>.log`
- Environment config in `.env` (see `.env.example`); `USE_REAL_COINBASE=true` switches from simulators to live Coinbase API
- Risk limits in `risk_limits.json` are editable at runtime via GUI; OM re-reads the file on every order check
- Symbols: `BTC/USD`, `ETH/USD`, `SOL/USD`, `ADA/USD`, `DOGE/USD`
- Frontend is vanilla HTML/JS/CSS (no build step, no framework)
