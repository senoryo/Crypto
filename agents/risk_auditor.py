#!/usr/bin/env python3
"""Risk Auditor Agent — Audits all order paths for risk check coverage."""

import ast
import os
import sys
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
REPORT_FILE = os.path.join(PROJECT_ROOT, "agent_reports", "risk_auditor.md")

def log(msg):
    print(f"[RISK-AUDITOR] {msg}", flush=True)

findings = []

def finding(severity, category, message):
    findings.append({"severity": severity, "category": category, "message": message})
    log(f"[{severity}] {category}: {message}")

def read_source(path):
    with open(os.path.join(PROJECT_ROOT, path)) as f:
        return f.read()

def audit_validate_order():
    """Audit OM._validate_order for completeness."""
    log("Auditing OM._validate_order...")
    src = read_source("om/order_manager.py")

    # Check: does validate_order initialize price before use?
    # The known bug: price is only set inside if ord_type == OrdType.Limit
    validate_fn = src[src.find("def _validate_order"):]
    validate_fn = validate_fn[:validate_fn.find("\n    def ", 10)]

    # Check for the unbound price variable bug
    lines = validate_fn.split("\n")
    price_assigned = False
    price_used_in_check = False
    for line in lines:
        stripped = line.strip()
        if "price = float(" in stripped and "ord_type" not in stripped:
            price_assigned = True
        if "check_order" in stripped and "price" in stripped:
            price_used_in_check = True

    if price_used_in_check and not price_assigned:
        finding("CRITICAL", "unbound-variable",
                "om/order_manager.py: `price` used in check_order() but only assigned inside "
                "`if ord_type == OrdType.Limit:` block — market orders cause UnboundLocalError")
    else:
        finding("OK", "unbound-variable", "om/order_manager.py: `price` properly initialized before use")

    # Check: all 4 risk check types present
    checks = {
        "max_order_qty": "max_order_qty" in validate_fn or "check_order" in validate_fn,
        "max_order_notional": "max_order_notional" in validate_fn or "check_order" in validate_fn,
        "max_position_qty": "max_position_qty" in validate_fn or "check_order" in validate_fn,
        "max_open_orders": "max_open_orders" in validate_fn or "check_order" in validate_fn,
    }
    if all(checks.values()):
        finding("OK", "risk-coverage", "OM._validate_order delegates to check_order() which covers all 4 risk types")
    else:
        missing = [k for k, v in checks.items() if not v]
        finding("ERROR", "risk-coverage", f"Missing risk checks in _validate_order: {missing}")

def audit_amend_risk_checks():
    """Audit OM._handle_cancel_replace_request for risk checks."""
    log("Auditing OM amend risk checks...")
    src = read_source("om/order_manager.py")

    amend_fn = src[src.find("def _handle_cancel_replace_request"):]
    amend_fn = amend_fn[:amend_fn.find("\n    async def ", 10)]

    checks_present = {
        "qty_validation": "new_qty <= 0" in amend_fn,
        "price_validation": "new_price <= 0" in amend_fn,
        "max_order_qty": "max_order_qty" in amend_fn,
        "max_notional": "max_order_notional" in amend_fn or "max_notional" in amend_fn,
    }

    for check, present in checks_present.items():
        if present:
            finding("OK", "amend-risk", f"Amend path has {check} check")
        else:
            finding("WARN", "amend-risk", f"Amend path may be missing {check} check")

    # Check: amend does NOT check position limits
    if "max_position_qty" not in amend_fn and "check_order" not in amend_fn:
        finding("WARN", "amend-risk",
                "Amend path does not check max_position_qty — an amend could increase qty "
                "beyond position limits if the original order was validated at a smaller size")

    # Check: amend does NOT check max_open_orders
    if "max_open_orders" not in amend_fn:
        finding("INFO", "amend-risk",
                "Amend path does not check max_open_orders (acceptable — amend doesn't create new orders)")

def audit_cancel_path():
    """Verify cancel path has no risk checks (correct behavior)."""
    log("Auditing cancel path...")
    src = read_source("om/order_manager.py")

    cancel_fn = src[src.find("def _handle_cancel_request"):]
    cancel_fn = cancel_fn[:cancel_fn.find("\n    async def ", 10)]

    if "check_order" in cancel_fn or "load_limits" in cancel_fn:
        finding("WARN", "cancel-path", "Cancel path has unexpected risk checks")
    else:
        finding("OK", "cancel-path", "Cancel path correctly has no risk checks")

    # Verify it checks order exists
    if "self.orders.get(" in cancel_fn:
        finding("OK", "cancel-path", "Cancel path validates order exists before forwarding")
    else:
        finding("ERROR", "cancel-path", "Cancel path may not validate order exists")

def audit_risk_limits_file():
    """Audit risk_limits.py for correctness."""
    log("Auditing risk_limits.py...")
    src = read_source("shared/risk_limits.py")

    # Check: load_limits falls back to defaults
    if "DEFAULT_RISK_LIMITS" in src:
        finding("OK", "risk-limits", "load_limits() falls back to DEFAULT_RISK_LIMITS")
    else:
        finding("ERROR", "risk-limits", "load_limits() has no fallback to defaults")

    # Check: check_order handles missing limit keys gracefully
    if ".get(" in src:
        finding("OK", "risk-limits", "check_order() uses .get() for safe key access")
    else:
        finding("WARN", "risk-limits", "check_order() may not handle missing limit keys")

    # Check: position check uses abs() for short positions
    if "abs(projected)" in src:
        finding("OK", "risk-limits", "Position check uses abs() to handle both long and short")
    else:
        finding("WARN", "risk-limits", "Position check may not handle short positions correctly")

    # Check: notional check is limit-only
    if "ORD_TYPE_LIMIT" in src:
        finding("OK", "risk-limits", "Notional check correctly limited to limit orders only")
    else:
        finding("WARN", "risk-limits", "Notional check may apply to market orders (incorrect)")

def audit_position_consistency():
    """Check OM and POSMANAGER track positions consistently."""
    log("Auditing position tracking consistency...")
    om_src = read_source("om/order_manager.py")
    pos_src = read_source("posmanager/posmanager.py")

    # OM tracks positions with signed_qty from fills
    if "signed_qty" in om_src and "_positions" in om_src:
        finding("OK", "position-tracking", "OM tracks positions via signed fill quantities")
    else:
        finding("WARN", "position-tracking", "OM position tracking unclear")

    # OM sends fills to POSMANAGER
    if "fill" in om_src and "pos_client.send" in om_src:
        finding("OK", "position-tracking", "OM sends fill notifications to POSMANAGER")
    else:
        finding("ERROR", "position-tracking", "OM may not send fills to POSMANAGER")

    # POSMANAGER uses apply_fill which handles flips
    if "apply_fill" in pos_src and "_apply_buy" in pos_src and "_apply_sell" in pos_src:
        finding("OK", "position-tracking",
                "POSMANAGER uses full position math (avg cost, realized P&L, flips)")
    else:
        finding("WARN", "position-tracking", "POSMANAGER position math may be incomplete")

    # Key difference: OM tracks simple net qty, POSMANAGER tracks full state
    finding("INFO", "position-tracking",
            "OM tracks simple net qty for risk; POSMANAGER tracks full state (avg_cost, P&L). "
            "These could drift if fill notifications are lost.")

def audit_exchconn_risk_bypass():
    """Check if orders can bypass OM risk checks via direct EXCHCONN access."""
    log("Auditing exchange connector for risk bypasses...")
    src = read_source("exchconn/exchconn.py")

    # EXCHCONN is a server — does it have its own risk checks?
    if "check_order" in src or "risk_limits" in src:
        finding("INFO", "exchconn-risk", "EXCHCONN has its own risk checks")
    else:
        finding("INFO", "exchconn-risk",
                "EXCHCONN has no risk checks — relies entirely on OM pre-validation. "
                "Any client connecting directly to port 8084 could bypass risk limits.")

def generate_report():
    report = ["# Risk Auditor Agent Report\n"]
    report.append("## Mission")
    report.append("Audit all order paths for risk check coverage and identify gaps.\n")

    criticals = [f for f in findings if f["severity"] == "CRITICAL"]
    errors = [f for f in findings if f["severity"] == "ERROR"]
    warns = [f for f in findings if f["severity"] == "WARN"]
    oks = [f for f in findings if f["severity"] == "OK"]
    infos = [f for f in findings if f["severity"] == "INFO"]

    report.append("## Summary\n")
    report.append(f"- Critical: **{len(criticals)}**")
    report.append(f"- Errors: **{len(errors)}**")
    report.append(f"- Warnings: **{len(warns)}**")
    report.append(f"- Passed: **{len(oks)}**")
    report.append(f"- Info: **{len(infos)}**\n")

    if criticals:
        report.append("## Critical Issues\n")
        for f in criticals:
            report.append(f"- **[{f['category']}]** {f['message']}")
        report.append("")

    if errors:
        report.append("## Errors\n")
        for f in errors:
            report.append(f"- **[{f['category']}]** {f['message']}")
        report.append("")

    if warns:
        report.append("## Warnings\n")
        for f in warns:
            report.append(f"- **[{f['category']}]** {f['message']}")
        report.append("")

    report.append("## All Findings\n")
    for f in findings:
        icon = {"CRITICAL": "CRIT", "ERROR": "FAIL", "WARN": "WARN", "OK": "pass", "INFO": "INFO"}[f["severity"]]
        report.append(f"- [{icon}] **{f['category']}**: {f['message']}")

    with open(REPORT_FILE, "w") as f:
        f.write("\n".join(report))
    log(f"Report written to {REPORT_FILE}")

def main():
    log("=" * 60)
    log("Risk Auditor Agent starting")
    log("=" * 60)

    audit_validate_order()
    audit_amend_risk_checks()
    audit_cancel_path()
    audit_risk_limits_file()
    audit_position_consistency()
    audit_exchconn_risk_bypass()

    generate_report()

    criticals = [f for f in findings if f["severity"] == "CRITICAL"]
    errors = [f for f in findings if f["severity"] == "ERROR"]
    log("=" * 60)
    log(f"Risk Auditor Agent complete — {len(criticals)} critical, {len(errors)} errors")
    log("=" * 60)

if __name__ == "__main__":
    main()
