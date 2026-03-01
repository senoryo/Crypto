#!/usr/bin/env python3
"""UX Reviewer Agent — Reviews the trading UI from an experienced trader's perspective.

Evaluates against four pillars: INTUITIVE, SIMPLE, INFORMATIVE, FUNCTIONAL.
"""

import os
import sys
import re
import json

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
REPORT_FILE = os.path.join(PROJECT_ROOT, "agent_reports", "ux_reviewer.md")

def log(msg):
    print(f"[UX-REVIEWER] {msg}", flush=True)

findings = []

def finding(pillar, severity, area, message):
    """Record a UX finding.

    pillar: INTUITIVE | SIMPLE | INFORMATIVE | FUNCTIONAL
    severity: GOOD | SUGGESTION | ISSUE | CRITICAL
    area: which part of the UI
    """
    findings.append({
        "pillar": pillar,
        "severity": severity,
        "area": area,
        "message": message,
    })
    log(f"[{severity}] [{pillar}] {area}: {message}")

def read_file(path):
    with open(os.path.join(PROJECT_ROOT, path)) as f:
        return f.read()


# ═══════════════════════════════════════════════════════════════
# REVIEW FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def review_order_entry():
    """Review the order entry workflow — the most critical trader interaction."""
    log("Reviewing order entry workflow...")
    html = read_file("gui/index.html")
    js = read_file("gui/app.js")
    css = read_file("gui/styles.css")

    # --- INTUITIVE ---

    # Check: does clicking a market data row populate the order entry?
    if "selectedSymbol" in js and "oe-symbol" in js and ("click" in js or "onclick" in js):
        finding("INTUITIVE", "GOOD", "Order Entry",
                "Clicking a market data row selects the symbol in order entry — "
                "traders expect click-to-trade")
    else:
        finding("INTUITIVE", "ISSUE", "Order Entry",
                "No click-to-trade: clicking a market data row should populate "
                "the symbol (and ideally the price) in the order entry form")

    # Check: does selecting a market data row populate the price field?
    if "oe-price" in js and ("last" in js or "ask" in js or "bid" in js):
        # Look for price auto-population on symbol selection
        if re.search(r'oe-price.*value.*=.*(bid|ask|last|price)', js, re.DOTALL):
            finding("INTUITIVE", "GOOD", "Order Entry",
                    "Price field auto-populates from market data on symbol click")
        elif "selectedSymbol" in js:
            finding("INTUITIVE", "SUGGESTION", "Order Entry",
                    "Price field does NOT auto-populate when clicking a market data row. "
                    "Traders expect: click BTC row → price fills with current ask (for buy) "
                    "or bid (for sell). This saves keystrokes and reduces errors")
    else:
        finding("INTUITIVE", "ISSUE", "Order Entry",
                "Price field has no connection to market data — traders must manually "
                "type prices, which is slow and error-prone")

    # Check: BUY/SELL toggle — is it prominent and color-coded?
    if "side-btn buy" in html and "side-btn sell" in html:
        finding("INTUITIVE", "GOOD", "Order Entry",
                "BUY/SELL toggle is prominent with dedicated buttons")
    if "--green" in css and "buy" in css and "--red" in css and "sell" in css:
        finding("INTUITIVE", "GOOD", "Order Entry",
                "BUY is green, SELL is red — universal trading color convention")

    # Check: submit button reflects current side
    if "oe-submit" in js and ("BUY" in js or "SELL" in js):
        submit_changes = "submit" in js and "buy" in js.lower() and "sell" in js.lower()
        if submit_changes:
            finding("INTUITIVE", "GOOD", "Order Entry",
                    "Submit button text and color change with BUY/SELL selection — "
                    "prevents accidental wrong-side orders")

    # Check: price field disabled for market orders
    if "onOrdTypeChange" in js or "ordtype" in js.lower():
        if "disabled" in js and "MARKET" in js:
            finding("INTUITIVE", "GOOD", "Order Entry",
                    "Price field is disabled for MARKET orders — prevents confusion")
        else:
            finding("INTUITIVE", "SUGGESTION", "Order Entry",
                    "Price field should be disabled/hidden for MARKET orders to avoid "
                    "confusion about what price will be used")

    # --- SIMPLE ---

    # Check: form field count (experienced traders want minimal fields)
    form_fields = html.count('<input type="number"') + html.count('<select id="oe-')
    if form_fields <= 6:
        finding("SIMPLE", "GOOD", "Order Entry",
                f"Order entry has {form_fields} fields — compact and efficient")
    else:
        finding("SIMPLE", "SUGGESTION", "Order Entry",
                f"Order entry has {form_fields} fields — consider reducing for faster entry")

    # Check: is there a keyboard shortcut or hotkey for submitting?
    if "keydown" in js or "keypress" in js or "hotkey" in js:
        finding("SIMPLE", "GOOD", "Order Entry",
                "Keyboard shortcuts available — essential for fast trading")
    else:
        finding("SIMPLE", "SUGGESTION", "Order Entry",
                "No keyboard shortcuts detected. Experienced traders rely on hotkeys: "
                "e.g., Enter to submit, B/S to toggle side, Esc to clear. "
                "This is a significant productivity gap for active traders")

    # Check: does the form clear after submission?
    if "reset" in js or ('""' in js and "qty" in js):
        finding("SIMPLE", "GOOD", "Order Entry",
                "Form clears after order submission — ready for next order")

    # --- FUNCTIONAL ---

    # Check: order confirmation or immediate send?
    if "confirm" in js.lower() and "order" in js.lower():
        finding("FUNCTIONAL", "SUGGESTION", "Order Entry",
                "Order confirmation dialog detected — good for safety but slows down "
                "experienced traders. Consider making it optional or only for large orders")
    else:
        finding("FUNCTIONAL", "GOOD", "Order Entry",
                "Orders submit immediately without confirmation — experienced traders "
                "prefer speed over confirmation dialogs")

    # Check: is there a notional value preview?
    if "notional" in js.lower() or "total" in js.lower() and "qty" in js.lower() and "price" in js.lower():
        finding("INFORMATIVE", "GOOD", "Order Entry",
                "Notional value preview shown — helps traders gauge order size in dollar terms")
    else:
        finding("INFORMATIVE", "SUGGESTION", "Order Entry",
                "No notional value preview (qty × price). Traders want to see "
                "'Buying 0.5 BTC ≈ $33,500' before submitting. This prevents "
                "fat-finger errors on high-value assets like BTC")

    # Check: spread display
    if "spread" in html.lower() or "oe-spread" in html or "spread-info" in html:
        finding("INFORMATIVE", "GOOD", "Order Entry",
                "Current spread displayed next to price field — traders can gauge execution cost")
    elif "spread" in js.lower():
        finding("INFORMATIVE", "SUGGESTION", "Order Entry",
                "Consider showing the current spread next to the price field "
                "so traders can gauge execution cost")


def review_market_data():
    """Review market data display from a trader's perspective."""
    log("Reviewing market data display...")
    html = read_file("gui/index.html")
    js = read_file("gui/app.js")
    css = read_file("gui/styles.css")

    # --- INFORMATIVE ---

    # Check: bid/ask/last columns
    md_cols = []
    for col in ["Symbol", "Bid", "Ask", "Last", "Volume", "Exch"]:
        if col in html:
            md_cols.append(col)
    finding("INFORMATIVE", "GOOD", "Market Data",
            f"Market data table has {len(md_cols)} columns: {', '.join(md_cols)}")

    # Check: is there a change/delta column?
    if "change" in html.lower() or "delta" in html.lower() or "%" in html:
        finding("INFORMATIVE", "GOOD", "Market Data",
                "Price change/delta column present — shows trend at a glance")
    else:
        finding("INFORMATIVE", "SUGGESTION", "Market Data",
                "No price change or % change column. Traders want to see at a glance: "
                "'BTC +2.3%' or 'ETH -$45.20'. This is one of the most-referenced "
                "data points on any trading screen")

    # Check: is there a high/low or 24h range?
    if "high" in html.lower() or "low" in html.lower() or "24h" in html.lower() or "range" in html.lower():
        finding("INFORMATIVE", "GOOD", "Market Data",
                "High/low or 24h range shown — gives traders context on price range")
    else:
        finding("INFORMATIVE", "SUGGESTION", "Market Data",
                "No high/low or 24h range displayed. Traders use this to gauge "
                "whether current price is near the top or bottom of the day's range")

    # Check: bid/ask size display (sizes may be rendered by JS inline, not as HTML columns)
    if "bid_size" in js or "ask_size" in js:
        if "bid_size" in html or "ask_size" in html or "Size" in html or "md-size" in js or "md-size" in css or "formatSize" in js:
            finding("INFORMATIVE", "GOOD", "Market Data",
                    "Bid/ask sizes shown inline — indicates market depth and liquidity")
        else:
            finding("INFORMATIVE", "SUGGESTION", "Market Data",
                    "Bid/ask sizes are in the data feed but not displayed in the table. "
                    "Showing size next to bid/ask helps traders gauge liquidity")

    # Check: color-coded price flashing
    if "flash" in css and ("green" in css or "red" in css):
        finding("INTUITIVE", "GOOD", "Market Data",
                "Price cells flash green/red on tick — instantly shows direction")

    # Check: mid-price or VWAP
    if "mid" in js.lower() or "vwap" in js.lower():
        finding("INFORMATIVE", "GOOD", "Market Data",
                "Mid-price or VWAP displayed")
    else:
        finding("INFORMATIVE", "SUGGESTION", "Market Data",
                "No mid-price displayed. The mid = (bid+ask)/2 is the fairest reference "
                "price and is useful when the spread is wide")

    # --- SIMPLE ---

    # Check: is the market data table compact?
    if "font-size: 12px" in css or "font-size: 11px" in css:
        finding("SIMPLE", "GOOD", "Market Data",
                "Compact font size — fits all symbols without scrolling")


def review_order_blotter():
    """Review the order blotter from a trader's perspective."""
    log("Reviewing order blotter...")
    html = read_file("gui/index.html")
    js = read_file("gui/app.js")
    css = read_file("gui/styles.css")

    # --- INFORMATIVE ---

    # Check blotter columns
    blotter_cols = []
    for col in ["ClOrdID", "Symbol", "Side", "Qty", "Price", "Type", "Status",
                "Filled", "AvgPx", "Exchange", "Actions"]:
        if col in html:
            blotter_cols.append(col)
    finding("INFORMATIVE", "GOOD", "Blotter",
            f"Order blotter has {len(blotter_cols)} columns: {', '.join(blotter_cols)}")

    # Check: is there a fill % or progress indicator?
    if "fill" in js.lower() and ("%" in js or "progress" in js.lower()):
        finding("INFORMATIVE", "GOOD", "Blotter",
                "Fill progress shown — traders can see partial fill status")
    else:
        finding("INFORMATIVE", "SUGGESTION", "Blotter",
                "No fill percentage shown. A '3/5 (60%)' or progress bar next to the "
                "Filled column helps traders instantly see how much of their order "
                "has been executed vs. what remains")

    # Check: status color coding
    status_classes = ["status-new", "status-partial", "status-filled",
                      "status-canceled", "status-rejected", "status-pending"]
    found_classes = [c for c in status_classes if c in css]
    if len(found_classes) >= 4:
        finding("INTUITIVE", "GOOD", "Blotter",
                f"Status is color-coded ({len(found_classes)} states) — "
                "traders can scan status at a glance")

    # Check: cancel and amend buttons
    if "btn-cancel" in html or "btn-cancel" in css:
        finding("FUNCTIONAL", "GOOD", "Blotter",
                "Cancel button available on active orders")
    if "btn-amend" in html or "btn-amend" in css:
        finding("FUNCTIONAL", "GOOD", "Blotter",
                "Amend button available on active orders — can modify qty/price without cancel+reenter")

    # Check: are actions disabled on terminal orders?
    if "disabled" in js and ("Filled" in js or "Canceled" in js or "Rejected" in js):
        finding("FUNCTIONAL", "GOOD", "Blotter",
                "Action buttons disabled on filled/canceled/rejected orders")
    else:
        finding("FUNCTIONAL", "SUGGESTION", "Blotter",
                "Verify that Cancel/Amend buttons are hidden or disabled on terminal-state "
                "orders (Filled, Canceled, Rejected) to prevent user confusion")

    # Check: order count display
    if "blotter-count" in html:
        finding("INFORMATIVE", "GOOD", "Blotter",
                "Order count displayed in panel header")

    # Check: sorting or filtering
    if "sort" in js.lower() or "filter" in js.lower():
        finding("FUNCTIONAL", "GOOD", "Blotter",
                "Sorting or filtering available in blotter")
    else:
        finding("FUNCTIONAL", "SUGGESTION", "Blotter",
                "No sorting or filtering on the blotter. With many orders, traders need "
                "to filter by: status (show only active), symbol, or side. "
                "At minimum, a 'show active only' toggle saves constant scanning")

    # Check: newest orders at top (could be via reverse(), prepend, insertBefore, or unshift)
    if "reverse()" in js or "prepend" in js or "insertBefore" in js or "unshift" in js:
        finding("INTUITIVE", "GOOD", "Blotter",
                "Newest orders appear at top — matches trader expectation")
    elif "append" in js or "push" in js:
        finding("INTUITIVE", "SUGGESTION", "Blotter",
                "Orders may appear at bottom. Traders expect newest orders at the top "
                "so they can see their latest activity without scrolling")


def review_positions():
    """Review the positions panel from a trader's perspective."""
    log("Reviewing positions panel...")
    html = read_file("gui/index.html")
    js = read_file("gui/app.js")
    css = read_file("gui/styles.css")

    # --- INFORMATIVE ---

    pos_cols = []
    for col in ["Symbol", "Qty", "Avg Cost", "Mkt Price", "Unrealized PnL", "Realized PnL"]:
        if col in html:
            pos_cols.append(col)
    finding("INFORMATIVE", "GOOD", "Positions",
            f"Position table has {len(pos_cols)} columns: {', '.join(pos_cols)}")

    # Check: P&L color coding
    if "pnl-positive" in css and "pnl-negative" in css:
        finding("INTUITIVE", "GOOD", "Positions",
                "P&L is color-coded green/red — instant visual on winning vs losing positions")

    # Check: total P&L summary
    if "total-upnl" in html and "total-rpnl" in html:
        finding("INFORMATIVE", "GOOD", "Positions",
                "Total unrealized and realized P&L shown in summary bar — "
                "traders need portfolio-level view at a glance")

    # Check: is there a total portfolio value?
    if "portfolio" in js.lower() or "net_value" in js.lower() or "equity" in js.lower():
        finding("INFORMATIVE", "GOOD", "Positions",
                "Portfolio value or equity displayed")
    else:
        finding("INFORMATIVE", "SUGGESTION", "Positions",
                "No total portfolio value / equity shown. Traders want to see "
                "total equity = cash + unrealized P&L at all times, not just P&L columns")

    # Check: position direction indicator (LONG/SHORT/FLAT)
    if "LONG" in js or "SHORT" in js or "FLAT" in js:
        finding("INTUITIVE", "GOOD", "Positions",
                "Position direction labeled (LONG/SHORT/FLAT)")
    else:
        finding("INTUITIVE", "SUGGESTION", "Positions",
                "No explicit LONG/SHORT label on positions. While qty sign implies "
                "direction, an explicit colored label ('LONG' in green, 'SHORT' in red) "
                "is faster to parse in a high-pressure moment")

    # Check: can you close a position from the positions panel?
    if "close" in js.lower() and "position" in js.lower():
        finding("FUNCTIONAL", "GOOD", "Positions",
                "Close position button available — one-click flatten")
    else:
        finding("FUNCTIONAL", "SUGGESTION", "Positions",
                "No 'Close Position' button on the positions panel. Traders frequently "
                "need to flatten a position quickly. Currently they must go to order entry, "
                "select the symbol, set the opposite side, enter the exact qty, and submit. "
                "A one-click 'Close' or 'Flatten' button on each position row would be "
                "a significant workflow improvement")

    # Check: position as % of portfolio
    if "weight" in js.lower() or "allocation" in js.lower() or "%" in html:
        finding("INFORMATIVE", "GOOD", "Positions",
                "Position weight or allocation % shown")


def review_layout_and_navigation():
    """Review overall layout, navigation, and visual hierarchy."""
    log("Reviewing layout and navigation...")
    html = read_file("gui/index.html")
    js = read_file("gui/app.js")
    css = read_file("gui/styles.css")

    # --- SIMPLE ---

    # Check: is it a single-page app?
    if "grid" in css and "panel" in css:
        finding("SIMPLE", "GOOD", "Layout",
                "Single-page grid layout — everything visible at once, no tab switching. "
                "This is the gold standard for trading terminals")

    # Check: left column = market data + order entry, right = blotter + positions
    if "left-col" in html and "grid-column" in html:
        finding("INTUITIVE", "GOOD", "Layout",
                "Left: Market Data + Order Entry, Right: Blotter + Positions — "
                "standard trading terminal layout. Data flows left-to-right: "
                "see prices → enter order → monitor fills → track position")

    # Check: header has system status
    if "status-btn" in html and "env-badge" in html:
        finding("INFORMATIVE", "GOOD", "Layout",
                "Header shows environment badge (SIM/SANDBOX/PRODUCTION) and system status — "
                "critical for knowing if you're trading real money")

    # Check: dark theme
    if "--bg-primary: #0d1117" in css or "dark" in css.lower():
        finding("SIMPLE", "GOOD", "Layout",
                "Dark theme — standard for trading terminals, reduces eye strain "
                "during extended sessions")

    # Check: monospace font for numbers
    if "font-mono" in css or "Consolas" in css or "monospace" in css:
        finding("SIMPLE", "GOOD", "Layout",
                "Monospace font used for numerical data — digits align vertically, "
                "making it easy to compare prices and quantities across rows")

    # --- FUNCTIONAL ---

    # Check: WebSocket reconnection
    if "reconnect" in js.lower() or "RECONNECT" in js:
        finding("FUNCTIONAL", "GOOD", "Connectivity",
                "WebSocket auto-reconnection — trading continues after brief network drops")

    # Check: toast notifications
    if "toast" in js.lower() and "toast" in html.lower():
        finding("INFORMATIVE", "GOOD", "Notifications",
                "Toast notifications for order events — non-blocking feedback "
                "that doesn't interrupt workflow")

    # Check: are fills highlighted or announced?
    if "fill" in js.lower() and ("toast" in js.lower() or "flash" in js.lower() or "notification" in js.lower()):
        finding("INFORMATIVE", "GOOD", "Notifications",
                "Fill events trigger notifications — traders need to know immediately "
                "when orders execute")

    # Check: error display
    if "error" in js.lower() and "toast" in js.lower():
        finding("FUNCTIONAL", "GOOD", "Error Handling",
                "Errors shown as toast notifications — visible but non-blocking")

    # Check: responsive or fixed layout?
    if "overflow: hidden" in css and "100vh" in css:
        finding("SIMPLE", "GOOD", "Layout",
                "Fixed viewport layout — no scrolling on main page, everything "
                "fits on screen like a professional terminal")


def review_modals_and_admin():
    """Review modal dialogs and administrative features."""
    log("Reviewing modals and administrative features...")
    html = read_file("gui/index.html")
    js = read_file("gui/app.js")

    # Check: amend modal
    if "amend-modal" in html and "amend-qty" in html and "amend-price" in html:
        finding("FUNCTIONAL", "GOOD", "Amend Modal",
                "Amend modal allows modifying qty and price — both fields editable")

    # Check: does amend pre-fill current values?
    if "amend-qty" in js and "amend-price" in js and "value" in js:
        finding("INTUITIVE", "GOOD", "Amend Modal",
                "Amend modal pre-fills current order values — reduces errors")

    # Check: risk limits modal
    if "risk-modal" in html and "saveRiskLimits" in js:
        finding("FUNCTIONAL", "GOOD", "Risk Limits",
                "Risk limits editable from the GUI — no restart required")

    # Check: system architecture modal
    if "status-modal" in html and "arch-component" in html:
        finding("INFORMATIVE", "GOOD", "System Status",
                "System architecture view with component status — shows which "
                "components are up/down with visual diagram")

    # Check: message records
    if "records-modal" in html and "records-component" in html:
        finding("FUNCTIONAL", "GOOD", "Records",
                "Message records viewer with component/direction filters — "
                "essential for debugging order flow issues")

    # Check: troubleshoot
    if "troubleshoot-modal" in html:
        finding("FUNCTIONAL", "GOOD", "Troubleshoot",
                "AI troubleshoot feature available — can query system issues naturally")


def review_trader_workflow():
    """Review the end-to-end workflow from a trader's perspective."""
    log("Reviewing end-to-end trader workflow...")
    js = read_file("gui/app.js")
    html = read_file("gui/index.html")

    # --- CRITICAL WORKFLOW GAPS ---

    # Check: can you double-click an order to amend it?
    if "dblclick" in js and "amend" in js.lower():
        finding("INTUITIVE", "GOOD", "Workflow",
                "Double-click an order to amend — natural interaction")
    else:
        finding("INTUITIVE", "SUGGESTION", "Workflow",
                "No double-click to amend on blotter rows. Traders expect to "
                "double-click an order to quickly amend it, rather than finding "
                "and clicking a small Amend button")

    # Check: is there an order history or trade log?
    if "history" in js.lower() or "trade_log" in js.lower() or "execution_log" in js.lower() or ("trades" in js.lower() and "tab" in js.lower()):
        finding("INFORMATIVE", "GOOD", "Workflow",
                "Trade history or execution log available")
    else:
        finding("INFORMATIVE", "SUGGESTION", "Workflow",
                "No dedicated trade/execution history. The blotter shows orders, but "
                "a separate 'Trades' tab showing individual fills (with timestamps, "
                "prices, fees) would help traders analyze execution quality")

    # Check: is there a P&L chart or equity curve?
    if "chart" in js.lower() or "canvas" in js.lower() or "svg" in js.lower():
        finding("INFORMATIVE", "GOOD", "Workflow",
                "Chart or visual P&L display available")
    else:
        finding("INFORMATIVE", "SUGGESTION", "Workflow",
                "No P&L chart or equity curve. A simple line chart showing cumulative "
                "P&L over time is one of the most-requested features by traders — "
                "it gives an instant read on session performance")

    # Check: cancel all button
    if "cancel_all" in js.lower() or "cancelAll" in js:
        finding("FUNCTIONAL", "GOOD", "Workflow",
                "Cancel All button available — critical panic button for traders")
    else:
        finding("FUNCTIONAL", "ISSUE", "Workflow",
                "No 'Cancel All Orders' button. This is a critical safety feature — "
                "when the market moves against you, you need to cancel everything "
                "with one click, not hunt through individual orders. "
                "Every professional trading terminal has this")

    # Check: flatten all positions
    if "flatten" in js.lower() or "close_all" in js.lower() or "flattenAll" in js:
        finding("FUNCTIONAL", "GOOD", "Workflow",
                "Flatten all positions button available")
    else:
        finding("FUNCTIONAL", "SUGGESTION", "Workflow",
                "No 'Flatten All' button. Combined with 'Cancel All', these two "
                "buttons form the trader's emergency toolkit: cancel all pending + "
                "flatten all positions = go completely flat instantly")

    # Check: order entry from position (reverse/double)
    if "reverse" in js.lower() or "double" in js.lower():
        finding("FUNCTIONAL", "GOOD", "Workflow",
                "Reverse/double position available from positions panel")

    # Check: time display / clock
    if "header-clock" in html or "header-clock" in js or "updateClock" in js:
        finding("INFORMATIVE", "GOOD", "Workflow",
                "Real-time clock displayed in header — traders reference time "
                "for market open/close and event timing")
    else:
        finding("INFORMATIVE", "SUGGESTION", "Workflow",
                "Consider adding a real-time clock in the header — traders reference "
                "time constantly for market open/close and event timing")


# ═══════════════════════════════════════════════════════════════
# REPORT GENERATION
# ═══════════════════════════════════════════════════════════════

def generate_report():
    report = ["# UX Reviewer Agent Report\n"]
    report.append("## Mission")
    report.append("Review the trading UI from an experienced trader's perspective.")
    report.append("Evaluated against four pillars: **INTUITIVE**, **SIMPLE**, **INFORMATIVE**, **FUNCTIONAL**.\n")

    goods = [f for f in findings if f["severity"] == "GOOD"]
    suggestions = [f for f in findings if f["severity"] == "SUGGESTION"]
    issues = [f for f in findings if f["severity"] == "ISSUE"]
    criticals = [f for f in findings if f["severity"] == "CRITICAL"]

    report.append("## Summary\n")
    report.append(f"- Strengths: **{len(goods)}**")
    report.append(f"- Suggestions: **{len(suggestions)}**")
    report.append(f"- Issues: **{len(issues)}**")
    report.append(f"- Critical: **{len(criticals)}**\n")

    # Score by pillar
    report.append("## Pillar Scores\n")
    for pillar in ["INTUITIVE", "SIMPLE", "INFORMATIVE", "FUNCTIONAL"]:
        pf = [f for f in findings if f["pillar"] == pillar]
        pg = len([f for f in pf if f["severity"] == "GOOD"])
        ps = len([f for f in pf if f["severity"] == "SUGGESTION"])
        pi = len([f for f in pf if f["severity"] in ("ISSUE", "CRITICAL")])
        total = len(pf)
        score = round(pg / total * 100) if total > 0 else 0
        report.append(f"- **{pillar}**: {score}% ({pg} good, {ps} suggestions, {pi} issues)")
    report.append("")

    # Critical and Issues first
    if criticals or issues:
        report.append("## Critical & Issues\n")
        for f in criticals + issues:
            icon = "CRIT" if f["severity"] == "CRITICAL" else "ISSUE"
            report.append(f"- [{icon}] **{f['area']}** [{f['pillar']}]: {f['message']}")
        report.append("")

    # Suggestions (the actionable stuff)
    if suggestions:
        report.append("## Suggestions (Prioritized)\n")

        # Group by priority: workflow > informative > intuitive > functional
        high = [f for f in suggestions if f["area"] in ("Workflow", "Order Entry")]
        medium = [f for f in suggestions if f["area"] in ("Market Data", "Blotter", "Positions")]
        low = [f for f in suggestions if f not in high and f not in medium]

        if high:
            report.append("### High Priority (Trader Workflow)\n")
            for f in high:
                report.append(f"- **{f['area']}** [{f['pillar']}]: {f['message']}")
            report.append("")

        if medium:
            report.append("### Medium Priority (Data & Display)\n")
            for f in medium:
                report.append(f"- **{f['area']}** [{f['pillar']}]: {f['message']}")
            report.append("")

        if low:
            report.append("### Lower Priority (Polish)\n")
            for f in low:
                report.append(f"- **{f['area']}** [{f['pillar']}]: {f['message']}")
            report.append("")

    # Strengths
    report.append("## Strengths\n")
    for f in goods:
        report.append(f"- **{f['area']}** [{f['pillar']}]: {f['message']}")
    report.append("")

    with open(REPORT_FILE, "w") as f:
        f.write("\n".join(report))
    log(f"Report written to {REPORT_FILE}")


def main():
    log("=" * 60)
    log("UX Reviewer Agent starting")
    log("Persona: Experienced crypto trader")
    log("Criteria: INTUITIVE | SIMPLE | INFORMATIVE | FUNCTIONAL")
    log("=" * 60)

    review_order_entry()
    review_market_data()
    review_order_blotter()
    review_positions()
    review_layout_and_navigation()
    review_modals_and_admin()
    review_trader_workflow()

    generate_report()

    suggestions = [f for f in findings if f["severity"] == "SUGGESTION"]
    issues = [f for f in findings if f["severity"] in ("ISSUE", "CRITICAL")]
    log("=" * 60)
    log(f"UX Reviewer complete — {len(issues)} issues, {len(suggestions)} suggestions")
    log("=" * 60)


if __name__ == "__main__":
    main()
