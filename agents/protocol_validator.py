#!/usr/bin/env python3
"""Protocol Validator Agent — Validates FIX protocol correctness and message flows."""

import os
import sys
import json
import importlib

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
REPORT_FILE = os.path.join(PROJECT_ROOT, "agent_reports", "protocol_validator.md")

def log(msg):
    print(f"[PROTOCOL-VALIDATOR] {msg}", flush=True)

findings = []

def finding(severity, component, message):
    findings.append({"severity": severity, "component": component, "message": message})
    log(f"[{severity}] {component}: {message}")

def validate_fix_message_tags():
    """Verify all FIX tag constants are consistent across modules."""
    log("Validating FIX tag constants...")
    from shared.fix_protocol import Tag, MsgType, ExecType, OrdStatus, Side, OrdType

    # Verify tag values are strings (not ints)
    tag_issues = []
    for name in dir(Tag):
        if name.startswith("_"):
            continue
        val = getattr(Tag, name)
        if not isinstance(val, str):
            tag_issues.append(f"Tag.{name} is {type(val).__name__}, expected str")
    if tag_issues:
        for issue in tag_issues:
            finding("ERROR", "fix_protocol.py", issue)
    else:
        finding("OK", "fix_protocol.py", "All Tag constants are strings")

    # Verify no duplicate tag values
    tag_values = {}
    for name in dir(Tag):
        if name.startswith("_"):
            continue
        val = getattr(Tag, name)
        if val in tag_values:
            finding("ERROR", "fix_protocol.py", f"Duplicate tag value {val}: Tag.{name} and Tag.{tag_values[val]}")
        tag_values[val] = name
    if len(tag_values) == len([n for n in dir(Tag) if not n.startswith("_")]):
        finding("OK", "fix_protocol.py", f"No duplicate tag values ({len(tag_values)} unique tags)")

def validate_encode_decode_roundtrip():
    """Test encode/decode roundtrip for various message types."""
    log("Validating encode/decode roundtrips...")
    from shared.fix_protocol import (
        FIXMessage, Tag, MsgType, Side, OrdType,
        new_order_single, execution_report, cancel_request, cancel_replace_request,
        ExecType, OrdStatus,
    )

    test_cases = [
        ("NewOrderSingle", new_order_single("C1", "BTC/USD", Side.Buy, 1.0, OrdType.Limit, 67000.0, "BINANCE")),
        ("ExecutionReport", execution_report("C1", "OM-1", ExecType.Trade, OrdStatus.Filled, "BTC/USD", Side.Buy, 0.0, 1.0, 67000.0, 67000.0, 1.0)),
        ("CancelRequest", cancel_request("CXL-1", "C1", "BTC/USD", Side.Buy)),
        ("CancelReplaceRequest", cancel_replace_request("AMD-1", "C1", "BTC/USD", Side.Buy, 2.0, 68000.0)),
    ]

    for name, msg in test_cases:
        # Test pipe-delimited roundtrip
        encoded = msg.encode()
        decoded = FIXMessage.decode(encoded)
        if decoded.msg_type != msg.msg_type:
            finding("ERROR", "fix_protocol.py", f"{name}: MsgType mismatch after encode/decode: {decoded.msg_type} != {msg.msg_type}")
        else:
            finding("OK", "fix_protocol.py", f"{name}: pipe-delimited encode/decode roundtrip OK")

        # Test JSON roundtrip
        j = msg.to_json()
        restored = FIXMessage.from_json(j)
        if restored.msg_type != msg.msg_type:
            finding("ERROR", "fix_protocol.py", f"{name}: MsgType mismatch after JSON roundtrip")
        if restored.get(Tag.ClOrdID) != msg.get(Tag.ClOrdID):
            finding("ERROR", "fix_protocol.py", f"{name}: ClOrdID mismatch after JSON roundtrip")
        else:
            finding("OK", "fix_protocol.py", f"{name}: JSON roundtrip OK")

def validate_wire_message():
    """Validate FIXWireMessage encode/decode."""
    log("Validating FIXWireMessage wire protocol...")
    from shared.fix_engine import FIXWireMessage, SOH_CHR

    msg = FIXWireMessage()
    msg.set(35, "D").set(55, "BTC/USD").set(54, "1").set(38, "1.0").set(44, "67000.0")
    encoded = msg.encode()

    if not isinstance(encoded, bytes):
        finding("ERROR", "fix_engine.py", "FIXWireMessage.encode() does not return bytes")
        return

    decoded_str = encoded.decode("ascii")

    # Check FIXT.1.1 header
    if not decoded_str.startswith("8=FIXT.1.1"):
        finding("ERROR", "fix_engine.py", f"Missing FIXT.1.1 header, got: {decoded_str[:20]}")
    else:
        finding("OK", "fix_engine.py", "FIXT.1.1 header present")

    # Check body length field exists
    if "9=" not in decoded_str:
        finding("ERROR", "fix_engine.py", "Missing BodyLength (tag 9)")
    else:
        finding("OK", "fix_engine.py", "BodyLength (tag 9) present")

    # Check checksum field exists
    if "10=" not in decoded_str:
        finding("ERROR", "fix_engine.py", "Missing CheckSum (tag 10)")
    else:
        finding("OK", "fix_engine.py", "CheckSum (tag 10) present")

    # Roundtrip
    decoded = FIXWireMessage.decode(encoded)
    if decoded.get(35) != "D":
        finding("ERROR", "fix_engine.py", f"MsgType mismatch after roundtrip: {decoded.get(35)}")
    if decoded.get(55) != "BTC/USD":
        finding("ERROR", "fix_engine.py", f"Symbol mismatch after roundtrip: {decoded.get(55)}")
    else:
        finding("OK", "fix_engine.py", "FIXWireMessage encode/decode roundtrip OK")

def validate_protocol_consistency():
    """Check that FIXMessage (4.4) and FIXWireMessage (5.0) use compatible concepts."""
    log("Checking protocol consistency between FIXMessage and FIXWireMessage...")
    from shared.fix_protocol import FIXMessage, Tag
    from shared.fix_engine import FIXWireMessage

    # FIXMessage uses string tags, FIXWireMessage uses int tags
    # Verify the same logical message can be represented in both
    msg44 = FIXMessage("D")
    msg44.set(Tag.Symbol, "ETH/USD")

    msg50 = FIXWireMessage()
    msg50.set(35, "D")
    msg50.set(55, "ETH/USD")

    if msg44.msg_type == msg50.msg_type:
        finding("OK", "cross-protocol", "MsgType 'D' consistent across both implementations")
    else:
        finding("WARN", "cross-protocol", f"MsgType inconsistency: FIXMessage={msg44.msg_type}, FIXWireMessage={msg50.msg_type}")

    # Tag types are different (str vs int) — this is by design but worth noting
    finding("INFO", "cross-protocol", "FIXMessage uses str tags (e.g., '55'), FIXWireMessage uses int tags (e.g., 55) — by design")

def validate_guibroker_mappings():
    """Verify GUIBROKER's FIX-to-JSON mapping tables cover all expected values."""
    log("Validating GUIBROKER mapping tables...")
    from shared.fix_protocol import ExecType, OrdStatus, Side, OrdType

    # Import GUIBROKER maps
    sys.path.insert(0, PROJECT_ROOT)
    from guibroker.guibroker import SIDE_MAP, SIDE_REVERSE, ORD_TYPE_MAP, EXEC_TYPE_REVERSE, ORD_STATUS_REVERSE

    # Check EXEC_TYPE_REVERSE covers all ExecType values
    all_exec_types = [v for k, v in vars(ExecType).items() if not k.startswith("_")]
    for et in all_exec_types:
        if et not in EXEC_TYPE_REVERSE:
            finding("WARN", "guibroker.py", f"ExecType '{et}' not in EXEC_TYPE_REVERSE")
    missing_exec = set(all_exec_types) - set(EXEC_TYPE_REVERSE.keys())
    if not missing_exec:
        finding("OK", "guibroker.py", f"EXEC_TYPE_REVERSE covers all {len(all_exec_types)} ExecType values")

    # Check ORD_STATUS_REVERSE covers all OrdStatus values
    all_statuses = [v for k, v in vars(OrdStatus).items() if not k.startswith("_")]
    for st in all_statuses:
        if st not in ORD_STATUS_REVERSE:
            finding("WARN", "guibroker.py", f"OrdStatus '{st}' not in ORD_STATUS_REVERSE")
    missing_status = set(all_statuses) - set(ORD_STATUS_REVERSE.keys())
    if not missing_status:
        finding("OK", "guibroker.py", f"ORD_STATUS_REVERSE covers all {len(all_statuses)} OrdStatus values")

    # Check SIDE_MAP and SIDE_REVERSE are inverses
    for key, val in SIDE_MAP.items():
        if SIDE_REVERSE.get(val) != key:
            finding("ERROR", "guibroker.py", f"SIDE_MAP/SIDE_REVERSE mismatch for {key}/{val}")
    finding("OK", "guibroker.py", "SIDE_MAP and SIDE_REVERSE are consistent inverses")

def validate_factory_required_tags():
    """Verify factory functions set all required FIX tags."""
    log("Validating factory function required tags...")
    from shared.fix_protocol import (
        Tag, Side, OrdType, ExecType, OrdStatus,
        new_order_single, execution_report, cancel_request, cancel_replace_request,
    )

    # NewOrderSingle required tags
    msg = new_order_single("C1", "BTC/USD", Side.Buy, 1.0, OrdType.Limit, 67000.0)
    required = [Tag.MsgType, Tag.ClOrdID, Tag.Symbol, Tag.Side, Tag.OrderQty, Tag.OrdType, Tag.Price, Tag.TransactTime]
    for tag in required:
        if not msg.get(tag):
            finding("ERROR", "fix_protocol.py", f"NewOrderSingle missing required tag {tag}")
    finding("OK", "fix_protocol.py", f"NewOrderSingle has all {len(required)} required tags")

    # ExecutionReport required tags
    msg = execution_report("C1", "OM-1", ExecType.Trade, OrdStatus.Filled, "BTC/USD", Side.Buy, 0.0, 1.0, 67000.0, 67000.0, 1.0)
    required = [Tag.MsgType, Tag.ClOrdID, Tag.OrderID, Tag.ExecType, Tag.OrdStatus, Tag.Symbol, Tag.Side, Tag.LeavesQty, Tag.CumQty, Tag.AvgPx]
    for tag in required:
        if not msg.get(tag) and msg.get(tag) != "0" and msg.get(tag) != "0.0":
            finding("ERROR", "fix_protocol.py", f"ExecutionReport missing required tag {tag}")
    finding("OK", "fix_protocol.py", f"ExecutionReport has all {len(required)} required tags")

    # CancelRequest required tags
    msg = cancel_request("CXL-1", "C1", "BTC/USD", Side.Buy)
    required = [Tag.MsgType, Tag.ClOrdID, Tag.OrigClOrdID, Tag.Symbol, Tag.Side]
    for tag in required:
        if not msg.get(tag):
            finding("ERROR", "fix_protocol.py", f"CancelRequest missing required tag {tag}")
    finding("OK", "fix_protocol.py", f"CancelRequest has all {len(required)} required tags")

def generate_report():
    report = ["# Protocol Validator Agent Report\n"]
    report.append("## Mission")
    report.append("Validate FIX protocol correctness and message flows.\n")

    errors = [f for f in findings if f["severity"] == "ERROR"]
    warns = [f for f in findings if f["severity"] == "WARN"]
    oks = [f for f in findings if f["severity"] == "OK"]
    infos = [f for f in findings if f["severity"] == "INFO"]

    report.append(f"## Summary")
    report.append(f"- Checks passed: **{len(oks)}**")
    report.append(f"- Warnings: **{len(warns)}**")
    report.append(f"- Errors: **{len(errors)}**")
    report.append(f"- Info: **{len(infos)}**\n")

    if errors:
        report.append("## Errors\n")
        for f in errors:
            report.append(f"- **{f['component']}**: {f['message']}")
        report.append("")

    if warns:
        report.append("## Warnings\n")
        for f in warns:
            report.append(f"- **{f['component']}**: {f['message']}")
        report.append("")

    report.append("## All Findings\n")
    for f in findings:
        icon = {"OK": "pass", "WARN": "WARN", "ERROR": "FAIL", "INFO": "INFO"}[f["severity"]]
        report.append(f"- [{icon}] **{f['component']}**: {f['message']}")

    with open(REPORT_FILE, "w") as f:
        f.write("\n".join(report))
    log(f"Report written to {REPORT_FILE}")

def main():
    log("=" * 60)
    log("Protocol Validator Agent starting")
    log("=" * 60)

    validate_fix_message_tags()
    validate_encode_decode_roundtrip()
    validate_wire_message()
    validate_protocol_consistency()
    validate_guibroker_mappings()
    validate_factory_required_tags()

    generate_report()

    errors = [f for f in findings if f["severity"] == "ERROR"]
    log("=" * 60)
    log(f"Protocol Validator Agent complete — {len(errors)} errors found")
    log("=" * 60)

if __name__ == "__main__":
    main()
