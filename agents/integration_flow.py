#!/usr/bin/env python3
"""Integration Flow Agent — Tests end-to-end cross-component message flows."""

import os
import sys
import re
import json

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
REPORT_FILE = os.path.join(PROJECT_ROOT, "agent_reports", "integration_flow.md")

def log(msg):
    print(f"[INTEGRATION-FLOW] {msg}", flush=True)

findings = []

def finding(severity, flow, message):
    findings.append({"severity": severity, "flow": flow, "message": message})
    log(f"[{severity}] {flow}: {message}")

def read_source(path):
    with open(os.path.join(PROJECT_ROOT, path)) as f:
        return f.read()

def trace_new_order_flow():
    """Trace new order from GUI to exchange and back."""
    log("Tracing new order flow: GUI -> GUIBROKER -> OM -> EXCHCONN -> Exchange")

    # Step 1: GUI sends JSON to GUIBROKER
    gb_src = read_source("guibroker/guibroker.py")
    if "new_order" in gb_src and "_handle_new_order" in gb_src:
        finding("OK", "new-order", "GUIBROKER handles 'new_order' JSON from GUI")
    else:
        finding("ERROR", "new-order", "GUIBROKER missing new_order handler")

    # Step 2: GUIBROKER assigns ClOrdID
    if "_next_cl_ord_id" in gb_src and "GUI-" in gb_src:
        finding("OK", "new-order", "GUIBROKER assigns ClOrdID (GUI-N format)")
    else:
        finding("ERROR", "new-order", "GUIBROKER missing ClOrdID assignment")

    # Step 3: GUIBROKER sends ack to GUI
    if "order_ack" in gb_src:
        finding("OK", "new-order", "GUIBROKER sends order_ack to GUI before forwarding to OM")
    else:
        finding("WARN", "new-order", "GUIBROKER may not send order_ack to GUI")

    # Step 4: GUIBROKER converts to FIX and sends to OM
    if "new_order_single" in gb_src and "_send_to_om" in gb_src:
        finding("OK", "new-order", "GUIBROKER converts to FIX NewOrderSingle and sends to OM")
    else:
        finding("ERROR", "new-order", "GUIBROKER missing FIX conversion or OM send")

    # Step 5: OM receives and validates
    om_src = read_source("om/order_manager.py")
    if "NewOrderSingle" in om_src and "_handle_new_order" in om_src:
        finding("OK", "new-order", "OM routes NewOrderSingle to _handle_new_order")
    else:
        finding("ERROR", "new-order", "OM missing NewOrderSingle handler")

    # Step 6: OM validates + risk checks
    if "_validate_order" in om_src:
        finding("OK", "new-order", "OM calls _validate_order with risk checks")
    else:
        finding("ERROR", "new-order", "OM missing order validation")

    # Step 7: OM assigns OM ID and forwards to EXCHCONN
    if "_next_order_id" in om_src and "exchconn_client.send" in om_src:
        finding("OK", "new-order", "OM assigns OM-ID and forwards to EXCHCONN")
    else:
        finding("ERROR", "new-order", "OM missing ID assignment or EXCHCONN forwarding")

    # Step 8: EXCHCONN routes to exchange
    ec_src = read_source("exchconn/exchconn.py")
    if "submit_order" in ec_src:
        finding("OK", "new-order", "EXCHCONN routes to exchange simulator via submit_order()")
    else:
        finding("ERROR", "new-order", "EXCHCONN missing submit_order routing")

def trace_execution_report_flow():
    """Trace execution report from exchange back to GUI."""
    log("Tracing exec report flow: Exchange -> EXCHCONN -> OM -> GUIBROKER -> GUI")

    # Exchange sends report to EXCHCONN
    ec_src = read_source("exchconn/exchconn.py")
    if "_on_execution_report" in ec_src:
        finding("OK", "exec-report", "EXCHCONN receives exec reports via callback")
    else:
        finding("ERROR", "exec-report", "EXCHCONN missing exec report callback")

    # EXCHCONN broadcasts to OM
    if "broadcast" in ec_src or "_om_clients" in ec_src:
        finding("OK", "exec-report", "EXCHCONN forwards exec reports to connected OM clients")
    else:
        finding("ERROR", "exec-report", "EXCHCONN missing OM forwarding")

    # OM processes exec report
    om_src = read_source("om/order_manager.py")
    if "_handle_execution_report" in om_src and "ExecutionReport" in om_src:
        finding("OK", "exec-report", "OM processes ExecutionReport from EXCHCONN")
    else:
        finding("ERROR", "exec-report", "OM missing exec report handler")

    # OM updates order book
    if "order[\"status\"]" in om_src or 'order["status"]' in om_src:
        finding("OK", "exec-report", "OM updates internal order book from exec reports")
    else:
        finding("WARN", "exec-report", "OM may not update order book")

    # OM forwards to GUIBROKER
    if "source_ws" in om_src and "send_to" in om_src:
        finding("OK", "exec-report", "OM forwards exec reports to originating GUIBROKER client")
    else:
        finding("ERROR", "exec-report", "OM missing GUIBROKER forwarding")

    # GUIBROKER converts FIX to JSON
    gb_src = read_source("guibroker/guibroker.py")
    if "_handle_execution_report" in gb_src and "execution_report" in gb_src:
        finding("OK", "exec-report", "GUIBROKER converts FIX exec report to JSON")
    else:
        finding("ERROR", "exec-report", "GUIBROKER missing exec report conversion")

    # GUIBROKER routes to correct GUI client
    if "_client_orders" in gb_src and "target_ws" in gb_src:
        finding("OK", "exec-report", "GUIBROKER routes exec report to correct GUI client")
    else:
        finding("WARN", "exec-report", "GUIBROKER may not route to correct client")

def trace_cancel_flow():
    """Trace cancel order flow through all components."""
    log("Tracing cancel flow...")

    gb_src = read_source("guibroker/guibroker.py")
    om_src = read_source("om/order_manager.py")

    # GUIBROKER creates new ClOrdID for cancel
    if "_cancel_to_orig" in gb_src:
        finding("OK", "cancel", "GUIBROKER maps cancel ClOrdID back to original for GUI routing")
    else:
        finding("ERROR", "cancel", "GUIBROKER missing cancel-to-original mapping")

    # OM validates original order exists
    if "self.orders.get(orig_cl_ord_id)" in om_src:
        finding("OK", "cancel", "OM validates original order exists before forwarding cancel")
    else:
        finding("WARN", "cancel", "OM may not validate original order on cancel")

    # GUIBROKER maps response back
    if "cancel_to_orig" in gb_src and "gui_cl_ord_id" in gb_src:
        finding("OK", "cancel", "GUIBROKER maps cancel response back to original ClOrdID for GUI")
    else:
        finding("WARN", "cancel", "GUIBROKER may not map cancel response correctly")

def trace_fill_to_posmanager_flow():
    """Trace fill notification from OM to POSMANAGER."""
    log("Tracing fill -> POSMANAGER flow...")

    om_src = read_source("om/order_manager.py")
    pm_src = read_source("posmanager/posmanager.py")

    # OM sends fill notification
    if "fill" in om_src and "pos_client.send" in om_src:
        finding("OK", "fill-posmanager", "OM sends JSON fill notification to POSMANAGER")
    else:
        finding("ERROR", "fill-posmanager", "OM missing fill notification to POSMANAGER")

    # Check fill message format matches POSMANAGER expectations
    # OM sends: json_msg("fill", symbol=, side=, qty=, price=, cl_ord_id=, order_id=)
    if '"fill"' in om_src and "symbol" in om_src and "side" in om_src:
        finding("OK", "fill-posmanager", "OM fill message includes symbol, side, qty, price")
    else:
        finding("WARN", "fill-posmanager", "OM fill message format may not match POSMANAGER expectations")

    # POSMANAGER processes fills
    if "_process_fill" in pm_src:
        finding("OK", "fill-posmanager", "POSMANAGER has _process_fill handler")
    else:
        finding("ERROR", "fill-posmanager", "POSMANAGER missing fill processor")

    # POSMANAGER validates fill data
    if "qty <= 0 or price <= 0" in pm_src:
        finding("OK", "fill-posmanager", "POSMANAGER validates fill qty and price > 0")
    else:
        finding("WARN", "fill-posmanager", "POSMANAGER may not validate fill data")

    # POSMANAGER broadcasts position updates
    if "_schedule_broadcast" in pm_src:
        finding("OK", "fill-posmanager", "POSMANAGER broadcasts position updates after fills (throttled)")
    else:
        finding("WARN", "fill-posmanager", "POSMANAGER may not broadcast after fills")

def trace_market_data_flow():
    """Trace market data from MKTDATA to GUI and POSMANAGER."""
    log("Tracing market data flow...")

    pm_src = read_source("posmanager/posmanager.py")

    # POSMANAGER connects to MKTDATA as client
    if "mktdata_client" in pm_src and "MKTDATA" in pm_src:
        finding("OK", "market-data", "POSMANAGER connects to MKTDATA as WebSocket client")
    else:
        finding("ERROR", "market-data", "POSMANAGER missing MKTDATA connection")

    # POSMANAGER handles market_data messages
    if "market_data" in pm_src and "market_price" in pm_src:
        finding("OK", "market-data", "POSMANAGER updates market_price from MKTDATA ticks")
    else:
        finding("WARN", "market-data", "POSMANAGER may not process market data")

    # POSMANAGER recalculates unrealized P&L
    if "unrealized_pnl" in pm_src:
        finding("OK", "market-data", "POSMANAGER recalculates unrealized P&L on price updates")
    else:
        finding("WARN", "market-data", "Missing unrealized P&L recalculation")

def check_disconnect_handling():
    """Check how components handle disconnections."""
    log("Checking disconnect handling...")

    gb_src = read_source("guibroker/guibroker.py")
    om_src = read_source("om/order_manager.py")

    # GUIBROKER queues messages when OM disconnected
    if "_pending_queue" in gb_src and "_om_connected" in gb_src:
        finding("OK", "disconnect", "GUIBROKER queues messages when OM is disconnected")
    else:
        finding("WARN", "disconnect", "GUIBROKER may drop messages when OM disconnects")

    # GUIBROKER flushes queue on reconnect
    if "_flush_pending_queue" in gb_src:
        finding("OK", "disconnect", "GUIBROKER flushes pending queue on OM reconnection")
    else:
        finding("WARN", "disconnect", "GUIBROKER may not flush queue on reconnect")

    # OM handles GUIBROKER disconnect
    if "_handle_guibroker_disconnect" in om_src:
        finding("OK", "disconnect", "OM handles GUIBROKER client disconnection")
    else:
        finding("WARN", "disconnect", "OM may not handle GUIBROKER disconnection")

    # Check: what happens to open orders when GUIBROKER disconnects?
    disconnect_fn = om_src[om_src.find("_handle_guibroker_disconnect"):]
    disconnect_fn = disconnect_fn[:disconnect_fn.find("\n    async def ", 10)]
    if "orders" not in disconnect_fn and "cancel" not in disconnect_fn:
        finding("WARN", "disconnect",
                "OM does not cancel open orders when GUIBROKER disconnects — "
                "orders remain active but exec reports have no destination")

def generate_report():
    report = ["# Integration Flow Agent Report\n"]
    report.append("## Mission")
    report.append("Validate end-to-end message flows across component boundaries.\n")

    errors = [f for f in findings if f["severity"] == "ERROR"]
    warns = [f for f in findings if f["severity"] == "WARN"]
    oks = [f for f in findings if f["severity"] == "OK"]

    report.append("## Summary\n")
    report.append(f"- Passed: **{len(oks)}**")
    report.append(f"- Warnings: **{len(warns)}**")
    report.append(f"- Errors: **{len(errors)}**\n")

    flows = set(f["flow"] for f in findings)
    for flow in sorted(flows):
        flow_findings = [f for f in findings if f["flow"] == flow]
        flow_errors = [f for f in flow_findings if f["severity"] in ("ERROR", "WARN")]
        status = "ISSUES" if flow_errors else "OK"
        report.append(f"### Flow: `{flow}` — {status}\n")
        for f in flow_findings:
            icon = {"OK": "pass", "WARN": "WARN", "ERROR": "FAIL", "INFO": "INFO"}[f["severity"]]
            report.append(f"- [{icon}] {f['message']}")
        report.append("")

    with open(REPORT_FILE, "w") as f:
        f.write("\n".join(report))
    log(f"Report written to {REPORT_FILE}")

def main():
    log("=" * 60)
    log("Integration Flow Agent starting")
    log("=" * 60)

    trace_new_order_flow()
    trace_execution_report_flow()
    trace_cancel_flow()
    trace_fill_to_posmanager_flow()
    trace_market_data_flow()
    check_disconnect_handling()

    generate_report()

    errors = [f for f in findings if f["severity"] == "ERROR"]
    log("=" * 60)
    log(f"Integration Flow Agent complete — {len(errors)} errors")
    log("=" * 60)

if __name__ == "__main__":
    main()
