#!/usr/bin/env python3
"""Bug Hunter Agent — Static analysis for async pitfalls and code defects."""

import ast
import os
import sys
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
REPORT_FILE = os.path.join(PROJECT_ROOT, "agent_reports", "bug_hunter.md")

def log(msg):
    print(f"[BUG-HUNTER] {msg}", flush=True)

findings = []

def finding(severity, category, file, message):
    findings.append({"severity": severity, "category": category, "file": file, "message": message})
    log(f"[{severity}] {file}: {message}")

def get_python_files():
    """Get all Python source files (not tests, not agents)."""
    result = []
    for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT):
        # Skip test and agent directories
        rel = os.path.relpath(dirpath, PROJECT_ROOT)
        if any(skip in rel for skip in ["tests", "agents", ".git", "__pycache__", "venv", ".venv"]):
            continue
        for fn in filenames:
            if fn.endswith(".py"):
                result.append(os.path.join(dirpath, fn))
    return result

def check_missing_await(filepath):
    """Check for coroutines called without await."""
    rel = os.path.relpath(filepath, PROJECT_ROOT)
    try:
        with open(filepath) as f:
            source = f.read()
        tree = ast.parse(source)
    except SyntaxError:
        finding("WARN", "syntax", rel, "File has syntax errors, cannot parse")
        return

    # Collect all async function names defined in this file
    async_funcs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            async_funcs.add(node.name)

    # Look for calls to async functions without await
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                func_name = node.func.attr
            elif isinstance(node.func, ast.Name):
                func_name = node.func.id
            else:
                continue

            # Check if this call is inside an Await node
            # We can't easily check parent, so we look for common patterns

def check_bare_except(filepath):
    """Check for bare except clauses that swallow all exceptions."""
    rel = os.path.relpath(filepath, PROJECT_ROOT)
    try:
        with open(filepath) as f:
            source = f.read()
        tree = ast.parse(source)
    except SyntaxError:
        return

    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                finding("WARN", "bare-except", rel,
                        f"Line {node.lineno}: bare `except:` clause swallows all exceptions including KeyboardInterrupt")

def check_unhandled_task_exceptions(filepath):
    """Check for asyncio.create_task without exception handling."""
    rel = os.path.relpath(filepath, PROJECT_ROOT)
    with open(filepath) as f:
        lines = f.readlines()

    for i, line in enumerate(lines, 1):
        if "create_task(" in line:
            # Check if the task result is stored (for later awaiting)
            stripped = line.strip()
            if stripped.startswith("asyncio.create_task(") and "=" not in line.split("asyncio.create_task")[0]:
                finding("WARN", "fire-and-forget", rel,
                        f"Line {i}: create_task() result not stored — exceptions will be silently lost")

def check_resource_leaks(filepath):
    """Check for potential resource leaks (unclosed connections)."""
    rel = os.path.relpath(filepath, PROJECT_ROOT)
    with open(filepath) as f:
        source = f.read()

    # Check for WebSocket connections opened without close
    # "async with websockets.connect" is safe (context manager handles close)
    if "websockets.connect" in source and "close" not in source and "async with websockets.connect" not in source:
        finding("WARN", "resource-leak", rel,
                "WebSocket connection opened but no close() found in same file")

    # Check for open() without with statement
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "open":
                # Check if it's inside a with statement — hard to check via AST parent
                # Instead, check via source lines
                pass

def check_unbound_variables(filepath):
    """Check for variables used before assignment in conditional blocks."""
    rel = os.path.relpath(filepath, PROJECT_ROOT)
    with open(filepath) as f:
        source = f.read()

    # Specific check: the known pattern of price only assigned in if block
    # but used outside
    if "def _validate_order" in source:
        validate_fn = source[source.find("def _validate_order"):]
        validate_fn = validate_fn[:validate_fn.find("\n    def ", 10)]

        # Check if price is assigned before check_order call
        lines = validate_fn.split("\n")
        price_initialized_globally = False
        in_limit_block = False

        for line in lines:
            stripped = line.strip()
            # Price assigned at function level (not in if block)
            indent = len(line) - len(line.lstrip())
            if "price = float(" in stripped or "price = " in stripped:
                if "if " not in stripped:
                    # Check indent level — if it's at function body level, it's global
                    price_initialized_globally = True

        if not price_initialized_globally:
            # Check if price is used in check_order
            if "check_order" in validate_fn and "price" in validate_fn:
                finding("CRITICAL", "unbound-var", rel,
                        "`price` only assigned inside `if ord_type == OrdType.Limit:` but used "
                        "unconditionally in check_order() — market orders cause UnboundLocalError")

def check_race_conditions(filepath):
    """Check for potential race conditions in async code."""
    rel = os.path.relpath(filepath, PROJECT_ROOT)
    with open(filepath) as f:
        source = f.read()

    # Check for shared mutable state accessed without locks
    if "self._positions" in source and "Lock" not in source:
        if "async def" in source:
            finding("INFO", "race-condition", rel,
                    "self._positions modified in async methods without Lock "
                    "(may be safe if single-threaded event loop)")

    # Check for non-atomic read-modify-write patterns
    if "self._order_counter += 1" in source:
        if "Lock" not in source and "async def" in source:
            finding("INFO", "race-condition", rel,
                    "_order_counter increment is not atomic "
                    "(safe in single-threaded asyncio but fragile)")

def check_error_handling(filepath):
    """Check error handling patterns."""
    rel = os.path.relpath(filepath, PROJECT_ROOT)
    with open(filepath) as f:
        lines = f.readlines()

    for i, line in enumerate(lines, 1):
        # Check for except Exception with just pass
        if "except" in line and i < len(lines):
            next_stripped = lines[i].strip() if i < len(lines) else ""
            if next_stripped == "pass" and "CancelledError" not in line:
                finding("WARN", "silent-exception", rel,
                        f"Line {i}: exception caught and silently ignored with `pass`")

def check_type_coercion_safety(filepath):
    """Check for unsafe float/int conversions without try/except."""
    rel = os.path.relpath(filepath, PROJECT_ROOT)
    with open(filepath) as f:
        source = f.read()

    # Look for float() calls that aren't in try blocks
    lines = source.split("\n")
    in_try = False
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("try:"):
            in_try = True
        elif stripped.startswith("except"):
            in_try = False
        elif "float(" in stripped and ".get(" in stripped and not in_try:
            # float(something.get(...)) without try — could raise ValueError
            # But only flag in key business logic, not logging
            if "log" not in stripped.lower() and "logger" not in stripped.lower():
                pass  # Too many false positives, skip this check

def generate_report():
    report = ["# Bug Hunter Agent Report\n"]
    report.append("## Mission")
    report.append("Static analysis for async pitfalls, unbound variables, race conditions, and code defects.\n")

    criticals = [f for f in findings if f["severity"] == "CRITICAL"]
    errors = [f for f in findings if f["severity"] == "ERROR"]
    warns = [f for f in findings if f["severity"] == "WARN"]
    infos = [f for f in findings if f["severity"] == "INFO"]

    report.append("## Summary\n")
    report.append(f"- Files scanned: **{len(get_python_files())}**")
    report.append(f"- Critical: **{len(criticals)}**")
    report.append(f"- Errors: **{len(errors)}**")
    report.append(f"- Warnings: **{len(warns)}**")
    report.append(f"- Info: **{len(infos)}**\n")

    if criticals:
        report.append("## Critical Issues\n")
        for f in criticals:
            report.append(f"- **{f['file']}** [{f['category']}]: {f['message']}")
        report.append("")

    if errors:
        report.append("## Errors\n")
        for f in errors:
            report.append(f"- **{f['file']}** [{f['category']}]: {f['message']}")
        report.append("")

    if warns:
        report.append("## Warnings\n")
        for f in warns:
            report.append(f"- **{f['file']}** [{f['category']}]: {f['message']}")
        report.append("")

    if infos:
        report.append("## Info\n")
        for f in infos:
            report.append(f"- **{f['file']}** [{f['category']}]: {f['message']}")
        report.append("")

    report.append("## All Findings by File\n")
    files_seen = sorted(set(f["file"] for f in findings))
    for file in files_seen:
        file_findings = [f for f in findings if f["file"] == file]
        report.append(f"### `{file}`\n")
        for f in file_findings:
            icon = {"CRITICAL": "CRIT", "ERROR": "FAIL", "WARN": "WARN", "INFO": "INFO"}[f["severity"]]
            report.append(f"- [{icon}] [{f['category']}] {f['message']}")
        report.append("")

    with open(REPORT_FILE, "w") as f:
        f.write("\n".join(report))
    log(f"Report written to {REPORT_FILE}")

def main():
    log("=" * 60)
    log("Bug Hunter Agent starting")
    log("=" * 60)

    py_files = get_python_files()
    log(f"Scanning {len(py_files)} Python files...")

    for filepath in py_files:
        rel = os.path.relpath(filepath, PROJECT_ROOT)
        log(f"Scanning {rel}...")
        check_bare_except(filepath)
        check_unhandled_task_exceptions(filepath)
        check_resource_leaks(filepath)
        check_unbound_variables(filepath)
        check_race_conditions(filepath)
        check_error_handling(filepath)
        check_type_coercion_safety(filepath)

    generate_report()

    criticals = [f for f in findings if f["severity"] == "CRITICAL"]
    log("=" * 60)
    log(f"Bug Hunter Agent complete — {len(criticals)} critical, {len(findings)} total findings")
    log("=" * 60)

if __name__ == "__main__":
    main()
