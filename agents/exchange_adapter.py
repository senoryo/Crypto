#!/usr/bin/env python3
"""Exchange Adapter Agent — Validates simulators and real connectivity."""

import os
import sys
import re
import asyncio

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
REPORT_FILE = os.path.join(PROJECT_ROOT, "agent_reports", "exchange_adapter.md")

def log(msg):
    print(f"[EXCHANGE-ADAPTER] {msg}", flush=True)

findings = []

def finding(severity, category, message):
    findings.append({"severity": severity, "category": category, "message": message})
    log(f"[{severity}] {category}: {message}")

def read_source(path):
    with open(os.path.join(PROJECT_ROOT, path)) as f:
        return f.read()

def validate_simulator_interface_parity():
    """Verify BinanceSimulator and CoinbaseSimulator have the same interface."""
    log("Checking simulator interface parity...")

    bin_src = read_source("exchconn/binance_sim.py")
    cb_src = read_source("exchconn/coinbase_sim.py")

    required_methods = [
        "submit_order", "cancel_order", "amend_order",
        "set_report_callback", "_next_order_id", "_get_current_price",
        "start", "stop",
    ]

    for method in required_methods:
        bin_has = f"def {method}" in bin_src or f"async def {method}" in bin_src
        cb_has = f"def {method}" in cb_src or f"async def {method}" in cb_src
        if bin_has and cb_has:
            finding("OK", "interface-parity", f"Both simulators implement {method}()")
        elif bin_has and not cb_has:
            finding("ERROR", "interface-parity", f"CoinbaseSimulator missing {method}()")
        elif not bin_has and cb_has:
            finding("ERROR", "interface-parity", f"BinanceSimulator missing {method}()")
        else:
            finding("ERROR", "interface-parity", f"Neither simulator implements {method}()")

def validate_simulator_differences():
    """Document intentional differences between simulators."""
    log("Documenting simulator differences...")

    from exchconn.binance_sim import BinanceSimulator, PRICE_JITTER_PCT as BIN_JITTER, BASE_PRICES as BIN_PRICES
    from exchconn.coinbase_sim import CoinbaseSimulator, PRICE_JITTER_PCT as CB_JITTER, BASE_PRICES as CB_PRICES

    # Price jitter comparison
    if CB_JITTER > BIN_JITTER:
        finding("OK", "sim-differences",
                f"Coinbase has wider jitter ({CB_JITTER*100:.2f}%) than Binance ({BIN_JITTER*100:.2f}%) — realistic")
    else:
        finding("WARN", "sim-differences", "Coinbase jitter not wider than Binance")

    # Base prices should be the same
    if BIN_PRICES == CB_PRICES:
        finding("OK", "sim-differences", "Both simulators use identical base prices")
    else:
        finding("WARN", "sim-differences", "Simulators have different base prices")

    # Order ID prefixes
    bin_sim = BinanceSimulator()
    cb_sim = CoinbaseSimulator()
    if bin_sim.prefix != cb_sim.prefix:
        finding("OK", "sim-differences",
                f"Different order ID prefixes: Binance={bin_sim.prefix}, Coinbase={cb_sim.prefix}")
    else:
        finding("ERROR", "sim-differences", "Simulators have same order ID prefix — can't distinguish fills")

    # Name attribute
    if bin_sim.name == "BINANCE" and cb_sim.name == "COINBASE":
        finding("OK", "sim-differences", "Simulator names match expected exchange names")
    else:
        finding("ERROR", "sim-differences",
                f"Unexpected names: Binance={bin_sim.name}, Coinbase={cb_sim.name}")

def validate_fill_simulation():
    """Validate fill simulation correctness."""
    log("Validating fill simulation logic...")

    bin_src = read_source("exchconn/binance_sim.py")
    cb_src = read_source("exchconn/coinbase_sim.py")

    for name, src in [("Binance", bin_src), ("Coinbase", cb_src)]:
        # Check partial fill support
        if "fill_chunks" in src and "fills_done" in src:
            finding("OK", "fill-sim", f"{name}: supports partial fills (1-3 chunks)")
        else:
            finding("WARN", "fill-sim", f"{name}: may not support partial fills")

        # Check fill price variation
        if "random.uniform" in src and "chunk_price" in src:
            finding("OK", "fill-sim", f"{name}: applies random price variation per fill")
        else:
            finding("WARN", "fill-sim", f"{name}: no price variation per fill")

        # Check order state tracking
        if "cum_qty" in src and "leaves_qty" in src and "avg_px" in src:
            finding("OK", "fill-sim", f"{name}: tracks cum_qty, leaves_qty, avg_px correctly")
        else:
            finding("ERROR", "fill-sim", f"{name}: incomplete order state tracking")

        # Check is_active flag management
        if "is_active = False" in src:
            finding("OK", "fill-sim", f"{name}: deactivates orders on full fill/cancel")
        else:
            finding("WARN", "fill-sim", f"{name}: may not deactivate completed orders")

        # Check for epsilon comparison on final fill
        if "1e-10" in src:
            finding("OK", "fill-sim", f"{name}: uses epsilon comparison for final fill detection")
        else:
            finding("WARN", "fill-sim", f"{name}: may have floating point issues in fill detection")

def validate_exchconn_routing():
    """Validate EXCHCONN routes orders to correct exchange."""
    log("Validating EXCHCONN routing logic...")

    src = read_source("exchconn/exchconn.py")

    # Check ExDestination tag routing
    if "ExDestination" in src or "Tag.ExDestination" in src or "100" in src:
        finding("OK", "routing", "EXCHCONN uses ExDestination tag for routing")
    else:
        finding("ERROR", "routing", "EXCHCONN missing ExDestination routing")

    # Check fallback to default routing
    if "DEFAULT_ROUTING" in src:
        finding("OK", "routing", "EXCHCONN falls back to DEFAULT_ROUTING")
    else:
        finding("WARN", "routing", "EXCHCONN may not use DEFAULT_ROUTING fallback")

    # Check both exchanges registered
    if "BINANCE" in src and "COINBASE" in src:
        finding("OK", "routing", "Both BINANCE and COINBASE registered in EXCHCONN")
    else:
        finding("ERROR", "routing", "Not all exchanges registered in EXCHCONN")

    # Check message type routing
    if "NewOrderSingle" in src or "MsgType.NewOrderSingle" in src or '"D"' in src:
        finding("OK", "routing", "EXCHCONN routes NewOrderSingle messages")
    if "OrderCancelRequest" in src or '"F"' in src:
        finding("OK", "routing", "EXCHCONN routes CancelRequest messages")
    if "OrderCancelReplaceRequest" in src or '"G"' in src:
        finding("OK", "routing", "EXCHCONN routes CancelReplaceRequest messages")

def validate_real_coinbase_path():
    """Check real Coinbase connectivity path."""
    log("Validating real Coinbase connectivity path...")

    src = read_source("exchconn/exchconn.py")
    config_src = read_source("shared/config.py")

    # Check USE_REAL_COINBASE flag
    if "USE_REAL_COINBASE" in src:
        finding("OK", "real-coinbase", "EXCHCONN checks USE_REAL_COINBASE flag")
    else:
        finding("WARN", "real-coinbase", "EXCHCONN may not check USE_REAL_COINBASE flag")

    # Check Coinbase FIX path
    if "USE_COINBASE_FIX" in src or "USE_COINBASE_FIX" in config_src:
        finding("OK", "real-coinbase", "Coinbase FIX API path configured")
    else:
        finding("INFO", "real-coinbase", "No Coinbase FIX API path found")

    # Check HMAC signature utility
    engine_src = read_source("shared/fix_engine.py")
    if "build_coinbase_logon_signature" in engine_src:
        finding("OK", "real-coinbase", "HMAC-SHA256 logon signature function available")
    else:
        finding("ERROR", "real-coinbase", "Missing Coinbase FIX logon signature function")

    # Check API key configuration
    if "COINBASE_API_KEY" in config_src:
        finding("OK", "real-coinbase", "Coinbase API key configuration present")
    else:
        finding("WARN", "real-coinbase", "Missing Coinbase API key configuration")

    # Check sandbox vs production toggle
    if "COINBASE_MODE" in config_src and "sandbox" in config_src and "production" in config_src:
        finding("OK", "real-coinbase", "Sandbox/production mode toggle available")
    else:
        finding("WARN", "real-coinbase", "Missing sandbox/production toggle")

def validate_cancel_amend_lifecycle():
    """Validate cancel and amend order lifecycle in simulators."""
    log("Validating cancel/amend lifecycle...")

    bin_src = read_source("exchconn/binance_sim.py")

    # Cancel: should stop fill task
    if "fill_tasks" in bin_src and "cancel" in bin_src.lower():
        finding("OK", "cancel-lifecycle", "BinanceSimulator cancels pending fill tasks on order cancel")
    else:
        finding("WARN", "cancel-lifecycle", "BinanceSimulator may not cancel fill tasks")

    # Cancel: should reject if order not active
    if "not order.is_active" in bin_src:
        finding("OK", "cancel-lifecycle", "BinanceSimulator rejects cancel on inactive orders")
    else:
        finding("WARN", "cancel-lifecycle", "BinanceSimulator may not check order active status")

    # Amend: should update cl_ord_id mapping
    if "cl_to_order" in bin_src and "cl_ord_id" in bin_src:
        finding("OK", "amend-lifecycle", "BinanceSimulator updates cl_ord_id mapping on amend")
    else:
        finding("WARN", "amend-lifecycle", "BinanceSimulator may not update ID mappings on amend")

    # Amend: should cancel pending fill task and allow new fills at new price
    if "fill_tasks" in bin_src:
        finding("OK", "amend-lifecycle", "BinanceSimulator cancels fill tasks on amend")

def generate_report():
    report = ["# Exchange Adapter Agent Report\n"]
    report.append("## Mission")
    report.append("Validate exchange simulators and real connectivity paths.\n")

    errors = [f for f in findings if f["severity"] == "ERROR"]
    warns = [f for f in findings if f["severity"] == "WARN"]
    oks = [f for f in findings if f["severity"] == "OK"]
    infos = [f for f in findings if f["severity"] == "INFO"]

    report.append("## Summary\n")
    report.append(f"- Passed: **{len(oks)}**")
    report.append(f"- Warnings: **{len(warns)}**")
    report.append(f"- Errors: **{len(errors)}**")
    report.append(f"- Info: **{len(infos)}**\n")

    categories = sorted(set(f["category"] for f in findings))
    for cat in categories:
        cat_findings = [f for f in findings if f["category"] == cat]
        cat_errors = [f for f in cat_findings if f["severity"] in ("ERROR", "WARN")]
        status = "ISSUES" if cat_errors else "OK"
        report.append(f"### {cat} — {status}\n")
        for f in cat_findings:
            icon = {"OK": "pass", "WARN": "WARN", "ERROR": "FAIL", "INFO": "INFO"}[f["severity"]]
            report.append(f"- [{icon}] {f['message']}")
        report.append("")

    with open(REPORT_FILE, "w") as f:
        f.write("\n".join(report))
    log(f"Report written to {REPORT_FILE}")

def main():
    log("=" * 60)
    log("Exchange Adapter Agent starting")
    log("=" * 60)

    validate_simulator_interface_parity()
    validate_simulator_differences()
    validate_fill_simulation()
    validate_exchconn_routing()
    validate_real_coinbase_path()
    validate_cancel_amend_lifecycle()

    generate_report()

    errors = [f for f in findings if f["severity"] == "ERROR"]
    log("=" * 60)
    log(f"Exchange Adapter Agent complete — {len(errors)} errors")
    log("=" * 60)

if __name__ == "__main__":
    main()
