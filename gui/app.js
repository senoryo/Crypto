// ============================================================
// Crypto Trading Terminal - Frontend Application
// ============================================================

(function () {
    "use strict";

    // --- Configuration ---
    const WS_URLS = (function () {
        const isLocal = location.hostname === "localhost" && location.port === "8080";
        if (isLocal) {
            return {
                MKTDATA: "ws://localhost:8081",
                GUIBROKER: "ws://localhost:8082",
                POSMANAGER: "ws://localhost:8085",
            };
        }
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        return {
            MKTDATA: proto + "//" + location.host + "/ws/mktdata",
            GUIBROKER: proto + "//" + location.host + "/ws/guibroker",
            POSMANAGER: proto + "//" + location.host + "/ws/posmanager",
        };
    })();

    const SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD", "ADA/USD", "DOGE/USD"];

    const RECONNECT_DELAY = 3000;

    // --- State ---
    const state = {
        side: "BUY",
        marketData: {},      // symbol -> { bid, ask, last, bid_size, ask_size, volume, exchange }
        orders: new Map(),   // cl_ord_id -> order object
        positions: [],       // array of position objects
        trades: [],          // array of fill events { time, symbol, side, qty, price, exchange }
        pnlHistory: [],      // array of { time, pnl } for equity curve
        sockets: {},         // name -> WebSocket
        selectedSymbol: null,
        blotterTab: "orders", // "orders" or "trades"
    };

    // --- DOM References ---
    const dom = {
        mdTbody: document.getElementById("md-tbody"),
        blotterTbody: document.getElementById("blotter-tbody"),
        blotterCount: document.getElementById("blotter-count"),
        tradesTbody: document.getElementById("trades-tbody"),
        ordersBody: document.getElementById("orders-body"),
        tradesBody: document.getElementById("trades-body"),
        posTbody: document.getElementById("pos-tbody"),
        totalUpnl: document.getElementById("total-upnl"),
        totalRpnl: document.getElementById("total-rpnl"),
        totalEquity: document.getElementById("total-equity"),
        orderForm: document.getElementById("order-form"),
        oeSymbol: document.getElementById("oe-symbol"),
        oeBuy: document.getElementById("oe-buy"),
        oeSell: document.getElementById("oe-sell"),
        oeOrdType: document.getElementById("oe-ordtype"),
        oeQty: document.getElementById("oe-qty"),
        oePrice: document.getElementById("oe-price"),
        oeExchange: document.getElementById("oe-exchange"),
        oeSubmit: document.getElementById("oe-submit"),
        oeSpread: document.getElementById("oe-spread"),
        oeNotional: document.getElementById("oe-notional"),
        amendModal: document.getElementById("amend-modal"),
        amendClordid: document.getElementById("amend-clordid"),
        amendInfo: document.getElementById("amend-info"),
        amendQty: document.getElementById("amend-qty"),
        amendPrice: document.getElementById("amend-price"),
        toastContainer: document.getElementById("toast-container"),
        headerClock: document.getElementById("header-clock"),
        pnlChart: document.getElementById("pnl-chart"),
    };

    // ============================================================
    // Real-Time Clock
    // ============================================================

    function updateClock() {
        var now = new Date();
        var h = String(now.getHours()).padStart(2, "0");
        var m = String(now.getMinutes()).padStart(2, "0");
        var s = String(now.getSeconds()).padStart(2, "0");
        dom.headerClock.textContent = h + ":" + m + ":" + s;
    }

    setInterval(updateClock, 1000);
    updateClock();

    // ============================================================
    // WebSocket Management
    // ============================================================

    function connectWS(name, url, onMessage) {
        function connect() {
            const ws = new WebSocket(url);

            ws.onopen = function () {
                console.log("[WS] Connected to " + name);
                state.sockets[name] = ws;
                updateHeaderStatus();
            };

            ws.onmessage = function (event) {
                try {
                    const data = JSON.parse(event.data);
                    onMessage(data);
                } catch (e) {
                    console.warn("[WS] Bad message from " + name, e);
                }
            };

            ws.onclose = function () {
                console.log("[WS] Disconnected from " + name);
                state.sockets[name] = null;
                updateHeaderStatus();
                setTimeout(connect, RECONNECT_DELAY);
            };

            ws.onerror = function () {
                ws.close();
            };
        }

        connect();
    }

    // ============================================================
    // Market Data
    // ============================================================

    function initMarketDataRows() {
        SYMBOLS.forEach(function (sym) {
            state.marketData[sym] = {
                bid: 0, ask: 0, last: 0,
                bid_size: 0, ask_size: 0,
                volume: 0, exchange: "--",
            };
        });
        renderMarketData();
    }

    function onMarketData(data) {
        if (data.type !== "market_data") return;
        var sym = data.symbol;
        if (!sym) return;

        var prev = state.marketData[sym] || {};
        var prevLast = prev.last || 0;

        state.marketData[sym] = {
            bid: data.bid || 0,
            ask: data.ask || 0,
            last: data.last || 0,
            bid_size: data.bid_size || 0,
            ask_size: data.ask_size || 0,
            volume: data.volume || 0,
            exchange: data.exchange || "--",
        };

        renderMarketDataRow(sym, prevLast);
        updateSpreadDisplay();
        updateNotionalDisplay();
    }

    function renderMarketData() {
        dom.mdTbody.innerHTML = "";
        SYMBOLS.forEach(function (sym) {
            renderMarketDataRow(sym, 0);
        });
    }

    function renderMarketDataRow(sym, prevLast) {
        var d = state.marketData[sym];
        var rowId = "md-row-" + sym.replace("/", "-");
        var existingRow = document.getElementById(rowId);

        var isSelected = state.selectedSymbol === sym;

        // Compute spread and mid
        var spread = (d.bid > 0 && d.ask > 0) ? (d.ask - d.bid) : 0;
        var mid = (d.bid > 0 && d.ask > 0) ? ((d.bid + d.ask) / 2) : 0;

        // Bid with size
        var bidHtml = '<span class="md-price-main">' + formatPrice(d.bid, sym) + '</span>';
        if (d.bid_size > 0) {
            bidHtml += '<span class="md-size">' + formatSizeCompact(d.bid_size) + '</span>';
        }

        // Ask with size
        var askHtml = '<span class="md-price-main">' + formatPrice(d.ask, sym) + '</span>';
        if (d.ask_size > 0) {
            askHtml += '<span class="md-size">' + formatSizeCompact(d.ask_size) + '</span>';
        }

        var html =
            '<td class="md-symbol">' + sym + "</td>" +
            '<td class="md-bid">' + bidHtml + "</td>" +
            '<td class="md-ask">' + askHtml + "</td>" +
            '<td class="md-spread">' + (spread > 0 ? formatPrice(spread, sym) : "--") + "</td>" +
            '<td class="md-last">' + formatPrice(d.last, sym) + "</td>" +
            '<td class="md-mid">' + (mid > 0 ? formatPrice(mid, sym) : "--") + "</td>" +
            '<td class="md-volume">' + formatQty(d.volume) + "</td>";

        if (existingRow) {
            existingRow.innerHTML = html;
            if (isSelected) existingRow.classList.add("selected");
            else existingRow.classList.remove("selected");

            // Flash on price change
            if (prevLast > 0 && d.last !== prevLast) {
                var cls = d.last > prevLast ? "flash-green" : "flash-red";
                existingRow.classList.add(cls);
                setTimeout(function () {
                    existingRow.classList.remove(cls);
                }, 400);
            }
        } else {
            var tr = document.createElement("tr");
            tr.id = rowId;
            tr.innerHTML = html;
            if (isSelected) tr.classList.add("selected");
            tr.addEventListener("click", function () {
                selectSymbol(sym);
            });
            dom.mdTbody.appendChild(tr);
        }
    }

    function selectSymbol(sym) {
        state.selectedSymbol = sym;
        dom.oeSymbol.value = sym;

        // Update market data row highlighting
        SYMBOLS.forEach(function (s) {
            var rowId = "md-row-" + s.replace("/", "-");
            var row = document.getElementById(rowId);
            if (row) {
                if (s === sym) row.classList.add("selected");
                else row.classList.remove("selected");
            }
        });

        // Populate price from market data
        var d = state.marketData[sym];
        if (d && dom.oeOrdType.value === "LIMIT") {
            if (state.side === "BUY" && d.bid > 0) {
                dom.oePrice.value = d.bid;
            } else if (state.side === "SELL" && d.ask > 0) {
                dom.oePrice.value = d.ask;
            }
        }

        updateSpreadDisplay();
        updateNotionalDisplay();
    }

    // ============================================================
    // Spread & Notional Display in Order Entry
    // ============================================================

    function updateSpreadDisplay() {
        var sym = dom.oeSymbol.value;
        var d = state.marketData[sym];
        if (d && d.bid > 0 && d.ask > 0) {
            var spread = d.ask - d.bid;
            dom.oeSpread.textContent = "(spread: " + formatPrice(spread, sym) + ")";
        } else {
            dom.oeSpread.textContent = "";
        }
    }

    function updateNotionalDisplay() {
        var sym = dom.oeSymbol.value;
        var qty = parseFloat(dom.oeQty.value) || 0;
        var price = parseFloat(dom.oePrice.value) || 0;

        // For market orders, use last price
        if (dom.oeOrdType.value === "MARKET") {
            var d = state.marketData[sym];
            if (d && d.last > 0) {
                price = d.last;
            }
        }

        if (qty > 0 && price > 0) {
            var notional = qty * price;
            dom.oeNotional.textContent = "Notional: $" + notional.toFixed(2);
        } else {
            dom.oeNotional.textContent = "";
        }
    }

    // Update notional on qty/price input change
    dom.oeQty.addEventListener("input", updateNotionalDisplay);
    dom.oePrice.addEventListener("input", updateNotionalDisplay);
    dom.oeSymbol.addEventListener("change", function () {
        updateSpreadDisplay();
        updateNotionalDisplay();
    });

    // ============================================================
    // Order Entry
    // ============================================================

    window.setSide = function (side) {
        state.side = side;
        if (side === "BUY") {
            dom.oeBuy.classList.add("active");
            dom.oeSell.classList.remove("active");
            dom.oeSubmit.textContent = "BUY";
            dom.oeSubmit.className = "submit-btn buy";
        } else {
            dom.oeSell.classList.add("active");
            dom.oeBuy.classList.remove("active");
            dom.oeSubmit.textContent = "SELL";
            dom.oeSubmit.className = "submit-btn sell";
        }
    };

    window.onOrdTypeChange = function () {
        var isLimit = dom.oeOrdType.value === "LIMIT";
        dom.oePrice.disabled = !isLimit;
        if (!isLimit) dom.oePrice.value = "";
        updateNotionalDisplay();
    };

    function submitOrder(e) {
        e.preventDefault();

        var ws = state.sockets.GUIBROKER;
        if (!ws || ws.readyState !== WebSocket.OPEN) {
            showToast("Not connected to broker", "error");
            return;
        }

        var symbol = dom.oeSymbol.value;
        var qty = parseFloat(dom.oeQty.value);
        var ordType = dom.oeOrdType.value;
        var price = ordType === "LIMIT" ? parseFloat(dom.oePrice.value) : 0;
        var exchange = dom.oeExchange.value;

        if (!qty || qty <= 0) {
            showToast("Enter a valid quantity", "error");
            return;
        }

        if (ordType === "LIMIT" && (!price || price <= 0)) {
            showToast("Enter a valid price for limit order", "error");
            return;
        }

        var msg = {
            type: "new_order",
            symbol: symbol,
            side: state.side,
            qty: qty,
            ord_type: ordType,
            price: price,
            exchange: exchange,
        };

        ws.send(JSON.stringify(msg));
        showToast(state.side + " " + qty + " " + symbol + " @ " + (ordType === "MARKET" ? "MKT" : formatPrice(price, symbol)), "info");

        // Reset qty
        dom.oeQty.value = "";
        updateNotionalDisplay();
    }

    dom.orderForm.addEventListener("submit", submitOrder);

    // ============================================================
    // Order Blotter (Execution Reports)
    // ============================================================

    function onExecutionReport(data) {
        if (data.type !== "execution_report") return;

        var clOrdId = data.cl_ord_id;
        if (!clOrdId) return;

        var existing = state.orders.get(clOrdId) || {};

        // Track fill if filled_qty increased
        var prevFilledQty = existing.filled_qty || 0;
        var newFilledQty = data.filled_qty != null ? data.filled_qty : prevFilledQty;

        var order = {
            cl_ord_id: clOrdId,
            order_id: data.order_id || existing.order_id || "",
            symbol: data.symbol || existing.symbol || "",
            side: data.side || existing.side || "",
            qty: data.qty != null ? data.qty : (existing.qty || 0),
            price: data.price != null ? data.price : (existing.price || 0),
            ord_type: data.ord_type || existing.ord_type || "",
            status: data.status || existing.status || "",
            filled_qty: newFilledQty,
            avg_px: data.avg_px != null ? data.avg_px : (existing.avg_px || 0),
            leaves_qty: data.leaves_qty != null ? data.leaves_qty : (existing.leaves_qty || 0),
            exchange: data.exchange || existing.exchange || "",
        };

        state.orders.set(clOrdId, order);

        // Record fill in trade history
        if (newFilledQty > prevFilledQty) {
            var fillQty = newFilledQty - prevFilledQty;
            var fillPrice = data.avg_px || data.price || 0;
            state.trades.unshift({
                time: new Date().toLocaleTimeString(),
                symbol: order.symbol,
                side: order.side,
                qty: fillQty,
                price: fillPrice,
                exchange: order.exchange,
            });
            renderTrades();
        }

        renderBlotter();
    }

    function renderBlotter() {
        var rows = [];
        state.orders.forEach(function (order) {
            rows.push(order);
        });

        // Newest first
        rows.reverse();

        dom.blotterCount.textContent = rows.length + " order" + (rows.length !== 1 ? "s" : "");

        dom.blotterTbody.innerHTML = rows.map(function (o) {
            var sideClass = o.side === "BUY" ? "side-buy" : "side-sell";
            var statusClass = "status-" + o.status.toLowerCase().replace(/_/g, "");
            var isActive = o.status === "NEW" || o.status === "PARTIALLY_FILLED" || o.status === "PENDING_NEW";
            var actions = "";
            if (isActive) {
                actions =
                    '<div class="order-actions">' +
                    '<button class="btn-sm btn-cancel" onclick="cancelOrder(\'' + escapeHtml(o.cl_ord_id) + '\')">Cancel</button>' +
                    '<button class="btn-sm btn-amend" onclick="openAmendModal(\'' + escapeHtml(o.cl_ord_id) + '\')">Amend</button>' +
                    "</div>";
            }

            // Fill percentage
            var filledDisplay = formatQty(o.filled_qty);
            if (o.qty > 0 && o.filled_qty > 0) {
                var pct = Math.round((o.filled_qty / o.qty) * 100);
                filledDisplay = formatQty(o.filled_qty) + "/" + formatQty(o.qty) + " (" + pct + "%)";
            } else if (o.qty > 0 && o.filled_qty === 0) {
                filledDisplay = "0/" + formatQty(o.qty);
            }

            var dblClickAttr = isActive ? ' ondblclick="openAmendModal(\'' + escapeHtml(o.cl_ord_id) + '\')"' : '';

            return (
                "<tr" + dblClickAttr + ">" +
                "<td>" + escapeHtml(shortId(o.cl_ord_id)) + "</td>" +
                "<td>" + escapeHtml(o.symbol) + "</td>" +
                '<td class="' + sideClass + '">' + escapeHtml(o.side) + "</td>" +
                "<td>" + formatQty(o.qty) + "</td>" +
                "<td>" + (o.price > 0 ? formatPrice(o.price, o.symbol) : "MKT") + "</td>" +
                "<td>" + escapeHtml(o.ord_type || "") + "</td>" +
                '<td class="' + statusClass + '">' + escapeHtml(o.status) + "</td>" +
                '<td class="filled-cell">' + filledDisplay + "</td>" +
                "<td>" + (o.avg_px > 0 ? formatPrice(o.avg_px, o.symbol) : "--") + "</td>" +
                "<td>" + escapeHtml(o.exchange || "") + "</td>" +
                "<td>" + actions + "</td>" +
                "</tr>"
            );
        }).join("");
    }

    // ============================================================
    // Trade History
    // ============================================================

    function renderTrades() {
        dom.tradesTbody.innerHTML = state.trades.map(function (t) {
            var sideClass = t.side === "BUY" ? "side-buy" : "side-sell";
            return (
                "<tr>" +
                '<td class="col-ts">' + escapeHtml(t.time) + "</td>" +
                "<td>" + escapeHtml(t.symbol) + "</td>" +
                '<td class="' + sideClass + '">' + escapeHtml(t.side) + "</td>" +
                "<td>" + formatQty(t.qty) + "</td>" +
                "<td>" + (t.price > 0 ? formatPrice(t.price, t.symbol) : "--") + "</td>" +
                "<td>" + escapeHtml(t.exchange || "") + "</td>" +
                "</tr>"
            );
        }).join("");
    }

    // ============================================================
    // Blotter Tab Switching
    // ============================================================

    window.switchBlotterTab = function (tab) {
        state.blotterTab = tab;
        var tabOrders = document.getElementById("tab-orders");
        var tabTrades = document.getElementById("tab-trades");

        if (tab === "orders") {
            tabOrders.classList.add("active");
            tabTrades.classList.remove("active");
            dom.ordersBody.style.display = "";
            dom.tradesBody.style.display = "none";
        } else {
            tabTrades.classList.add("active");
            tabOrders.classList.remove("active");
            dom.ordersBody.style.display = "none";
            dom.tradesBody.style.display = "";
        }
    };

    // ============================================================
    // Cancel All Orders
    // ============================================================

    window.cancelAllOrders = function () {
        var ws = state.sockets.GUIBROKER;
        if (!ws || ws.readyState !== WebSocket.OPEN) {
            showToast("Not connected to broker", "error");
            return;
        }

        // Count active orders first
        var activeCount = 0;
        state.orders.forEach(function (order) {
            var isActive = order.status === "NEW" || order.status === "PARTIALLY_FILLED" || order.status === "PENDING_NEW";
            if (isActive) activeCount++;
        });

        if (activeCount === 0) {
            showToast("No active orders to cancel", "info");
            return;
        }

        if (!confirm("Cancel " + activeCount + " active order" + (activeCount !== 1 ? "s" : "") + "?")) {
            return;
        }

        var cancelCount = 0;
        state.orders.forEach(function (order) {
            var isActive = order.status === "NEW" || order.status === "PARTIALLY_FILLED" || order.status === "PENDING_NEW";
            if (isActive) {
                var msg = {
                    type: "cancel_order",
                    cl_ord_id: order.cl_ord_id,
                    symbol: order.symbol,
                    side: order.side,
                };
                ws.send(JSON.stringify(msg));
                cancelCount++;
            }
        });

        if (cancelCount > 0) {
            showToast("Canceling " + cancelCount + " order" + (cancelCount !== 1 ? "s" : ""), "info");
        }
    };

    // ============================================================
    // Cancel / Amend Single Order
    // ============================================================

    window.cancelOrder = function (clOrdId) {
        var ws = state.sockets.GUIBROKER;
        if (!ws || ws.readyState !== WebSocket.OPEN) {
            showToast("Not connected to broker", "error");
            return;
        }

        var order = state.orders.get(clOrdId);
        if (!order) return;

        var msg = {
            type: "cancel_order",
            cl_ord_id: clOrdId,
            symbol: order.symbol,
            side: order.side,
        };

        ws.send(JSON.stringify(msg));
        showToast("Cancel requested: " + shortId(clOrdId), "info");
    };

    // ============================================================
    // Amend Modal
    // ============================================================

    window.openAmendModal = function (clOrdId) {
        var order = state.orders.get(clOrdId);
        if (!order) return;

        // Only allow amend on active orders
        var isActive = order.status === "NEW" || order.status === "PARTIALLY_FILLED" || order.status === "PENDING_NEW";
        if (!isActive) return;

        dom.amendClordid.value = clOrdId;
        dom.amendInfo.value = order.side + " " + order.symbol + " (" + shortId(clOrdId) + ")";
        dom.amendQty.value = order.qty;
        dom.amendPrice.value = order.price || "";
        dom.amendModal.classList.add("active");
    };

    window.closeAmendModal = function () {
        dom.amendModal.classList.remove("active");
    };

    window.submitAmend = function () {
        var ws = state.sockets.GUIBROKER;
        if (!ws || ws.readyState !== WebSocket.OPEN) {
            showToast("Not connected to broker", "error");
            return;
        }

        var clOrdId = dom.amendClordid.value;
        var order = state.orders.get(clOrdId);
        if (!order) return;

        var newQty = parseFloat(dom.amendQty.value);
        var newPrice = parseFloat(dom.amendPrice.value);

        if (!newQty || newQty <= 0) {
            showToast("Enter a valid quantity", "error");
            return;
        }

        var msg = {
            type: "amend_order",
            cl_ord_id: clOrdId,
            symbol: order.symbol,
            side: order.side,
            qty: newQty,
            price: newPrice || 0,
        };

        ws.send(JSON.stringify(msg));
        showToast("Amend requested: " + shortId(clOrdId), "info");
        closeAmendModal();
    };

    // Close modal on overlay click
    dom.amendModal.addEventListener("click", function (e) {
        if (e.target === dom.amendModal) closeAmendModal();
    });

    // ============================================================
    // Positions
    // ============================================================

    function onPositionUpdate(data) {
        if (data.type !== "position_update") return;
        if (!Array.isArray(data.positions)) return;

        state.positions = data.positions;
        renderPositions();
    }

    function renderPositions() {
        var totalUpnl = 0;
        var totalRpnl = 0;

        dom.posTbody.innerHTML = state.positions.map(function (p) {
            var upnl = p.unrealized_pnl || 0;
            var rpnl = p.realized_pnl || 0;
            totalUpnl += upnl;
            totalRpnl += rpnl;

            var qty = p.qty || 0;
            var dirLabel = "";
            var dirClass = "";
            if (qty > 0) {
                dirLabel = "LONG";
                dirClass = "dir-long";
            } else if (qty < 0) {
                dirLabel = "SHORT";
                dirClass = "dir-short";
            } else {
                dirLabel = "FLAT";
                dirClass = "dir-flat";
            }

            return (
                "<tr>" +
                "<td>" + escapeHtml(p.symbol || "") + "</td>" +
                '<td class="' + dirClass + '">' + dirLabel + "</td>" +
                "<td>" + formatQty(Math.abs(qty)) + "</td>" +
                "<td>" + formatPrice(p.avg_cost, p.symbol) + "</td>" +
                "<td>" + formatPrice(p.market_price, p.symbol) + "</td>" +
                '<td class="' + pnlClass(upnl) + '">' + formatPnl(upnl) + "</td>" +
                '<td class="' + pnlClass(rpnl) + '">' + formatPnl(rpnl) + "</td>" +
                "</tr>"
            );
        }).join("");

        var totalEquity = totalUpnl + totalRpnl;

        dom.totalUpnl.textContent = formatPnl(totalUpnl);
        dom.totalUpnl.className = pnlClass(totalUpnl);
        dom.totalRpnl.textContent = formatPnl(totalRpnl);
        dom.totalRpnl.className = pnlClass(totalRpnl);
        dom.totalEquity.textContent = formatPnl(totalEquity);
        dom.totalEquity.className = pnlClass(totalEquity);

        // Track P&L history for chart
        var now = Date.now();
        if (state.pnlHistory.length === 0 || now - state.pnlHistory[state.pnlHistory.length - 1].time > 2000) {
            state.pnlHistory.push({ time: now, pnl: totalEquity });
            // Keep last 300 data points
            if (state.pnlHistory.length > 300) {
                state.pnlHistory.shift();
            }
            renderPnlChart();
        }
    }

    // ============================================================
    // Flatten All Positions
    // ============================================================

    window.flattenAll = function () {
        var ws = state.sockets.GUIBROKER;
        if (!ws || ws.readyState !== WebSocket.OPEN) {
            showToast("Not connected to broker", "error");
            return;
        }

        // Count open positions first
        var openCount = 0;
        state.positions.forEach(function (p) {
            if ((p.qty || 0) !== 0) openCount++;
        });

        if (openCount === 0) {
            showToast("No open positions to flatten", "info");
            return;
        }

        if (!confirm("Flatten " + openCount + " open position" + (openCount !== 1 ? "s" : "") + " with market orders?")) {
            return;
        }

        var flattenCount = 0;
        state.positions.forEach(function (p) {
            var qty = p.qty || 0;
            if (qty === 0) return;

            var side = qty > 0 ? "SELL" : "BUY";
            var closeQty = Math.abs(qty);

            var msg = {
                type: "new_order",
                symbol: p.symbol,
                side: side,
                qty: closeQty,
                ord_type: "MARKET",
                price: 0,
                exchange: "AUTO",
            };

            ws.send(JSON.stringify(msg));
            flattenCount++;
        });

        if (flattenCount > 0) {
            showToast("Flattening " + flattenCount + " position" + (flattenCount !== 1 ? "s" : ""), "info");
        }
    };

    // ============================================================
    // P&L Chart (Equity Curve)
    // ============================================================

    function renderPnlChart() {
        var canvas = dom.pnlChart;
        if (!canvas) return;
        var ctx = canvas.getContext("2d");
        var w = canvas.width;
        var h = canvas.height;

        ctx.clearRect(0, 0, w, h);

        var data = state.pnlHistory;
        if (data.length < 2) return;

        // Find min/max P&L
        var minPnl = Infinity, maxPnl = -Infinity;
        data.forEach(function (d) {
            if (d.pnl < minPnl) minPnl = d.pnl;
            if (d.pnl > maxPnl) maxPnl = d.pnl;
        });

        var range = maxPnl - minPnl;
        if (range === 0) range = 1;

        var padding = 2;
        var chartH = h - padding * 2;
        var chartW = w - padding * 2;

        // Draw zero line
        var zeroY = padding + chartH - ((0 - minPnl) / range) * chartH;
        if (minPnl <= 0 && maxPnl >= 0) {
            ctx.strokeStyle = "rgba(139, 148, 158, 0.3)";
            ctx.lineWidth = 1;
            ctx.setLineDash([3, 3]);
            ctx.beginPath();
            ctx.moveTo(padding, zeroY);
            ctx.lineTo(w - padding, zeroY);
            ctx.stroke();
            ctx.setLineDash([]);
        }

        // Draw P&L line
        var lastPnl = data[data.length - 1].pnl;
        ctx.strokeStyle = lastPnl >= 0 ? "#3fb950" : "#f85149";
        ctx.lineWidth = 1.5;
        ctx.beginPath();

        for (var i = 0; i < data.length; i++) {
            var x = padding + (i / (data.length - 1)) * chartW;
            var y = padding + chartH - ((data[i].pnl - minPnl) / range) * chartH;
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        }
        ctx.stroke();

        // Fill area under/above zero
        ctx.globalAlpha = 0.1;
        ctx.fillStyle = lastPnl >= 0 ? "#3fb950" : "#f85149";
        ctx.lineTo(w - padding, zeroY);
        ctx.lineTo(padding, zeroY);
        ctx.closePath();
        ctx.fill();
        ctx.globalAlpha = 1;
    }

    // ============================================================
    // GUIBROKER message handler (both execution reports + other)
    // ============================================================

    function onBrokerMessage(data) {
        if (data.type === "execution_report") {
            onExecutionReport(data);
        } else if (data.type === "order_ack") {
            // Pre-populate the order in the blotter from the ack
            var id = data.cl_ord_id;
            if (id && !orders[id]) {
                orders[id] = {
                    cl_ord_id: id,
                    symbol: data.symbol || "",
                    side: data.side || "",
                    qty: data.qty || 0,
                    price: data.price || 0,
                    ord_type: data.ord_type || "",
                    status: "PENDING_NEW",
                    filled_qty: 0,
                    avg_px: 0,
                    leaves_qty: data.qty || 0,
                    exchange: data.exchange || "",
                    text: "",
                };
                renderBlotter();
            }
        }
        // Could handle other broker message types here
    }

    // ============================================================
    // Formatting Helpers
    // ============================================================

    function formatPrice(price, symbol) {
        if (price == null || price === 0) return "--";
        // Determine decimals based on typical price ranges
        var p = parseFloat(price);
        if (symbol && (symbol.indexOf("DOGE") >= 0 || symbol.indexOf("ADA") >= 0)) {
            return p.toFixed(5);
        }
        if (p >= 1000) return p.toFixed(2);
        if (p >= 1) return p.toFixed(4);
        return p.toFixed(6);
    }

    function formatQty(qty) {
        if (qty == null || qty === 0) return "0";
        var q = parseFloat(qty);
        if (q === Math.floor(q) && q < 1e9) return q.toString();
        return q.toFixed(4);
    }

    function formatSizeCompact(size) {
        if (size == null || size === 0) return "";
        var s = parseFloat(size);
        if (s >= 1000) return (s / 1000).toFixed(1) + "k";
        if (s === Math.floor(s)) return s.toString();
        return s.toFixed(2);
    }

    function formatPnl(val) {
        if (val == null) return "0.00";
        var v = parseFloat(val);
        var prefix = v >= 0 ? "+" : "";
        return prefix + v.toFixed(2);
    }

    function pnlClass(val) {
        if (val > 0) return "pnl-positive";
        if (val < 0) return "pnl-negative";
        return "pnl-zero";
    }

    function shortId(id) {
        if (!id) return "--";
        if (id.length <= 10) return id;
        return id.substring(0, 8) + "..";
    }

    function escapeHtml(str) {
        if (!str) return "";
        return str.toString()
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    // ============================================================
    // Toast Notifications
    // ============================================================

    function showToast(message, type) {
        type = type || "info";
        var toast = document.createElement("div");
        toast.className = "toast " + type;
        toast.textContent = message;
        dom.toastContainer.appendChild(toast);

        setTimeout(function () {
            toast.style.opacity = "0";
            toast.style.transition = "opacity 0.3s";
            setTimeout(function () {
                if (toast.parentNode) toast.parentNode.removeChild(toast);
            }, 300);
        }, 3000);
    }

    // ============================================================
    // Config Modal
    // ============================================================

    window.openConfigModal = function () {
        var modal = document.getElementById("config-modal");
        var body = document.getElementById("config-body");
        modal.classList.add("active");
        body.innerHTML = '<span style="color:var(--text-muted)">Loading...</span>';

        fetch("/api/config")
            .then(function (r) { return r.json(); })
            .then(function (cfg) { body.innerHTML = renderConfig(cfg); })
            .catch(function (e) { body.innerHTML = '<span style="color:var(--red)">Failed to load config: ' + escapeHtml(e.message) + '</span>'; });
    };

    window.closeConfigModal = function () {
        document.getElementById("config-modal").classList.remove("active");
    };

    document.getElementById("config-modal").addEventListener("click", function (e) {
        if (e.target === document.getElementById("config-modal")) closeConfigModal();
    });

    function renderConfig(cfg) {
        var h = "";

        // System overview
        var modeClass = cfg.system.mode === "FIX" ? "fix" : (cfg.system.mode === "REAL" ? "real" : "sim");
        var cbModeClass = cfg.coinbase.mode === "production" ? "production" : "sandbox";
        var keyClass = cfg.coinbase.api_key_configured ? "yes" : "no";

        h += '<div class="cfg-section">';
        h += '<div class="cfg-section-title">System</div>';
        h += cfgRow("Trading Mode", '<span class="cfg-badge ' + modeClass + '">' + escapeHtml(cfg.system.mode) + '</span>');
        h += cfgRow("Coinbase Mode", '<span class="cfg-badge ' + cbModeClass + '">' + escapeHtml(cfg.coinbase.mode.toUpperCase()) + '</span>');

        // Show FIX or REST API key status depending on mode
        if (cfg.coinbase_fix && cfg.coinbase_fix.enabled) {
            var fixKeyClass = cfg.coinbase_fix.api_key_configured ? "yes" : "no";
            h += cfgRow("FIX Connectivity", '<span class="cfg-badge fix">ENABLED</span>');
            h += cfgRow("FIX API Key", '<span class="cfg-badge ' + fixKeyClass + '">' + (cfg.coinbase_fix.api_key_configured ? "YES" : "NO") + '</span>' + ' &nbsp; ' + escapeHtml(cfg.coinbase_fix.api_key_name));
        } else {
            h += cfgRow("API Key Configured", '<span class="cfg-badge ' + keyClass + '">' + (cfg.coinbase.api_key_configured ? "YES" : "NO") + '</span>');
            h += cfgRow("API Key Name", escapeHtml(cfg.coinbase.api_key_name));
        }
        h += "</div>";

        // Adapters
        h += '<div class="cfg-section">';
        h += '<div class="cfg-section-title">Active Adapters</div>';
        for (var exch in cfg.adapters) {
            var a = cfg.adapters[exch];
            h += cfgRow(exch + " Market Data", escapeHtml(a.market_data));
            h += cfgRow(exch + " Exchange", escapeHtml(a.exchange));
        }
        h += "</div>";

        // Endpoints — show FIX endpoints when enabled, otherwise REST/WS
        h += '<div class="cfg-section">';
        if (cfg.coinbase_fix && cfg.coinbase_fix.enabled) {
            h += '<div class="cfg-section-title">Coinbase FIX Endpoints</div>';
            h += cfgRow("Order Entry", escapeHtml(cfg.coinbase_fix.ord_host + ":" + cfg.coinbase_fix.port));
            h += cfgRow("Market Data", escapeHtml(cfg.coinbase_fix.md_host + ":" + cfg.coinbase_fix.port));
        } else {
            h += '<div class="cfg-section-title">Coinbase Endpoints</div>';
            h += cfgRow("REST API", escapeHtml(cfg.coinbase.rest_url));
            h += cfgRow("WS Market", escapeHtml(cfg.coinbase.ws_market_url));
            h += cfgRow("WS User", escapeHtml(cfg.coinbase.ws_user_url));
        }
        h += "</div>";

        // Components
        h += '<div class="cfg-section">';
        h += '<div class="cfg-section-title">Components</div>';
        for (var comp in cfg.components) {
            var c = cfg.components[comp];
            h += cfgRow(comp, escapeHtml(c.url));
        }
        h += "</div>";

        // Routing
        h += '<div class="cfg-section">';
        h += '<div class="cfg-section-title">Default Routing</div>';
        for (var sym in cfg.routing) {
            h += cfgRow(sym, escapeHtml(cfg.routing[sym]));
        }
        h += "</div>";

        return h;
    }

    function cfgRow(label, value) {
        return '<div class="cfg-row"><span class="cfg-label">' + label + '</span><span class="cfg-value">' + value + '</span></div>';
    }

    // ============================================================
    // Status Modal (Architecture Diagram)
    // ============================================================

    var COMPONENT_INFO = {
        GUI:        { port: 8080, role: "Web UI server" },
        MKTDATA:    { port: 8081, role: "Market data feed" },
        GUIBROKER:  { port: 8082, role: "Order gateway" },
        OM:         { port: 8083, role: "Order manager" },
        EXCHCONN:   { port: 8084, role: "Exchange connector" },
        POSMANAGER: { port: 8085, role: "Position tracker" },
    };

    window.openStatusModal = function () {
        var modal = document.getElementById("status-modal");
        var body = document.getElementById("status-body");
        modal.classList.add("active");
        body.innerHTML = '<span style="color:var(--text-muted)">Loading...</span>';

        fetch("/api/status")
            .then(function (r) { return r.json(); })
            .then(function (resp) {
                var serverComps = resp.components || resp;
                var exchanges = resp.exchanges || {};
                var fixSessions = resp.fix_sessions || null;
                // Merge: for WS-connected components, use client-side state
                var WS_COMPONENTS = { MKTDATA: true, GUIBROKER: true, POSMANAGER: true };
                var merged = {};
                for (var name in COMPONENT_INFO) {
                    if (name === "GUI") {
                        merged[name] = true;
                    } else if (WS_COMPONENTS[name]) {
                        // WS-connected component: use client readyState
                        var ws = state.sockets[name];
                        merged[name] = ws && ws.readyState === WebSocket.OPEN;
                    } else {
                        // Server-probed (OM, EXCHCONN)
                        merged[name] = !!serverComps[name];
                    }
                }
                body.innerHTML = renderStatusDiagram(merged, exchanges, fixSessions);
            })
            .catch(function (e) {
                body.innerHTML = '<span style="color:var(--red)">Failed to load status: ' + escapeHtml(e.message) + '</span>';
            });
    };

    window.closeStatusModal = function () {
        document.getElementById("status-modal").classList.remove("active");
    };

    document.getElementById("status-modal").addEventListener("click", function (e) {
        if (e.target === document.getElementById("status-modal")) closeStatusModal();
    });

    function componentBox(name, isUp) {
        var info = COMPONENT_INFO[name];
        var cls = isUp ? "up" : "down";
        return '<div class="arch-component ' + cls + '">' +
            '<div class="arch-component-name"><span class="arch-dot ' + cls + '"></span>' + name + '</div>' +
            '<div class="arch-port">:' + info.port + '</div>' +
            '<div class="arch-role">' + info.role + '</div>' +
            '</div>';
    }

    function arrow(label, dir) {
        var line = dir === "left" ? "◀──" : "──▶";
        return '<div class="arch-arrow">' +
            '<div class="arch-arrow-line">' + line + '</div>' +
            '<div class="arch-arrow-label">' + label + '</div>' +
            '</div>';
    }

    function exchangeBox(name, info) {
        var mode = info && info.mode ? info.mode : "UNKNOWN";
        var displayMode = mode;
        var modeClass;
        if (mode === "SIMULATOR") {
            modeClass = "sim";
        } else if (mode === "PRODUCTION") {
            modeClass = "production";
        } else if (mode === "FIX_SANDBOX") {
            modeClass = "fix";
            displayMode = "FIX SBX";
        } else if (mode === "FIX_PRODUCTION") {
            modeClass = "fix-prod";
            displayMode = "FIX PROD";
        } else {
            modeClass = "sandbox";
        }
        return '<div class="arch-exchange">' +
            '<div class="arch-exchange-name">' + name + '</div>' +
            '<div class="arch-mode-badge ' + modeClass + '">' + displayMode + '</div>' +
            '</div>';
    }

    function fixSessionBadge(session) {
        if (!session) return '<span class="fix-session-badge disconnected">DISCONNECTED</span>';
        if (session.logged_in) return '<span class="fix-session-badge logged-in">LOGGED IN</span>';
        if (session.connected) return '<span class="fix-session-badge connecting">CONNECTING</span>';
        var reason = session.last_logout_reason;
        if (reason) return '<span class="fix-session-badge rejected" title="' + escapeHtml(reason) + '">REJECTED: ' + escapeHtml(reason) + '</span>';
        return '<span class="fix-session-badge disconnected">DISCONNECTED</span>';
    }

    function renderStatusDiagram(status, exchanges, fixSessions) {
        var h = "";

        // Determine if Coinbase is using FIX connectivity
        var cbMode = exchanges.COINBASE && exchanges.COINBASE.mode || "";
        var isFIX = cbMode.indexOf("FIX") === 0;
        var exchArrowLabel = isFIX ? "FIX 5.0" : "REST/WS";
        var feedArrowLabel = isFIX ? "FIX 5.0" : "feeds";

        // FIX session status panel (shown when FIX is enabled)
        if (isFIX && fixSessions) {
            h += '<div class="fix-sessions-panel">';
            h += '<div class="cfg-section-title">FIX Sessions</div>';
            var ordSession = fixSessions["FIX-ORD"] || null;
            var mdSession = fixSessions["FIX-MD"] || null;
            h += '<div class="fix-session-row">';
            h += '<span class="fix-session-label">Order Entry</span>';
            h += fixSessionBadge(ordSession);
            if (ordSession && ordSession.host) h += '<span class="fix-session-host">' + escapeHtml(ordSession.host) + '</span>';
            h += '</div>';
            h += '<div class="fix-session-row">';
            h += '<span class="fix-session-label">Market Data</span>';
            h += fixSessionBadge(mdSession);
            if (mdSession && mdSession.host) h += '<span class="fix-session-host">' + escapeHtml(mdSession.host) + '</span>';
            h += '</div>';
            h += '</div>';
        }

        // Row 1: GUI → GUIBROKER → OM → EXCHCONN → Exchanges
        h += '<div class="arch-row">';
        h += componentBox("GUI", status.GUI);
        h += arrow("JSON", "right");
        h += componentBox("GUIBROKER", status.GUIBROKER);
        h += arrow("FIX", "right");
        h += componentBox("OM", status.OM);
        h += arrow("FIX", "right");
        h += componentBox("EXCHCONN", status.EXCHCONN);
        h += arrow(exchArrowLabel, "right");
        h += '<div class="arch-exchange-group">';
        h += exchangeBox("BINANCE", exchanges.BINANCE);
        h += exchangeBox("COINBASE", exchanges.COINBASE);
        h += '</div>';
        h += '</div>';

        // Vertical connectors
        h += '<div class="arch-vertical-section">';
        h += '<div class="arch-vconn">';
        h += '<div class="arch-vconn-line">│</div>';
        h += '<div class="arch-vconn-label">fills</div>';
        h += '<div class="arch-vconn-line">▼</div>';
        h += '</div>';
        h += '</div>';

        // Row 2: POSMANAGER ← MKTDATA ← feeds
        h += '<div class="arch-row">';
        h += componentBox("POSMANAGER", status.POSMANAGER);
        h += arrow("market data", "left");
        h += componentBox("MKTDATA", status.MKTDATA);
        h += arrow(feedArrowLabel, "left");
        h += '<div class="arch-exchange-group">';
        h += exchangeBox("BINANCE", exchanges.BINANCE);
        h += exchangeBox("COINBASE", exchanges.COINBASE);
        h += '</div>';
        h += '</div>';

        // Legend
        h += '<div class="arch-legend">';
        h += '<div class="arch-legend-item"><span class="arch-dot up"></span> Connected</div>';
        h += '<div class="arch-legend-item"><span class="arch-dot down"></span> Disconnected</div>';
        h += '<div class="arch-legend-item"><span class="arch-mode-badge sim" style="font-size:9px;padding:1px 5px">SIM</span> Simulated</div>';
        h += '<div class="arch-legend-item"><span class="arch-mode-badge sandbox" style="font-size:9px;padding:1px 5px">SBX</span> Sandbox</div>';
        h += '<div class="arch-legend-item"><span class="arch-mode-badge production" style="font-size:9px;padding:1px 5px">PROD</span> Production</div>';
        h += '<div class="arch-legend-item"><span class="arch-mode-badge fix" style="font-size:9px;padding:1px 5px">FIX</span> FIX 5.0 SP2</div>';
        h += '</div>';

        return h;
    }

    // ============================================================
    // Header Status (button color + env badge)
    // ============================================================

    function updateHeaderStatus() {
        var btn = document.getElementById("status-btn");
        var WS_NAMES = ["MKTDATA", "GUIBROKER", "POSMANAGER"];
        var upCount = 0;
        var total = WS_NAMES.length;

        WS_NAMES.forEach(function (name) {
            var ws = state.sockets[name];
            if (ws && ws.readyState === WebSocket.OPEN) upCount++;
        });

        btn.classList.remove("all-up", "some-down", "all-down");
        if (upCount === total) {
            btn.classList.add("all-up");
        } else if (upCount === 0) {
            btn.classList.add("all-down");
        } else {
            btn.classList.add("some-down");
        }
    }

    // Fetch server status to also probe OM/EXCHCONN and get env badge
    function pollFullStatus() {
        fetch("/api/status")
            .then(function (r) { return r.json(); })
            .then(function (resp) {
                var comps = resp.components || {};
                var exchanges = resp.exchanges || {};

                // Update button: merge WS state + server probes
                var WS_COMPONENTS = { MKTDATA: true, GUIBROKER: true, POSMANAGER: true };
                var allNames = ["MKTDATA", "GUIBROKER", "POSMANAGER", "OM", "EXCHCONN"];
                var upCount = 0;

                allNames.forEach(function (name) {
                    var isUp;
                    if (WS_COMPONENTS[name]) {
                        var ws = state.sockets[name];
                        isUp = ws && ws.readyState === WebSocket.OPEN;
                    } else {
                        isUp = !!comps[name];
                    }
                    if (isUp) upCount++;
                });

                var btn = document.getElementById("status-btn");
                btn.classList.remove("all-up", "some-down", "all-down");
                if (upCount === allNames.length) {
                    btn.classList.add("all-up");
                } else if (upCount === 0) {
                    btn.classList.add("all-down");
                } else {
                    btn.classList.add("some-down");
                }

                // Update env badge
                var badge = document.getElementById("env-badge");
                var modes = [];
                for (var exch in exchanges) {
                    if (exchanges[exch] && exchanges[exch].mode) {
                        modes.push(exchanges[exch].mode);
                    }
                }

                var env, envClass;
                if (modes.indexOf("FIX_PRODUCTION") >= 0) {
                    env = "FIX PRODUCTION";
                    envClass = "fix-prod";
                } else if (modes.indexOf("PRODUCTION") >= 0) {
                    env = "PRODUCTION";
                    envClass = "production";
                } else if (modes.indexOf("FIX_SANDBOX") >= 0) {
                    env = "FIX SANDBOX";
                    envClass = "fix";
                } else if (modes.indexOf("SANDBOX") >= 0) {
                    env = "SANDBOX";
                    envClass = "sandbox";
                } else {
                    env = "SIMULATOR";
                    envClass = "sim";
                }

                badge.textContent = env;
                badge.className = "env-badge " + envClass;
            })
            .catch(function () {
                // Server unreachable — mark all down
                var btn = document.getElementById("status-btn");
                btn.classList.remove("all-up", "some-down", "all-down");
                btn.classList.add("all-down");
            });
    }

    // Poll every 10 seconds for full status (OM, EXCHCONN, env)
    pollFullStatus();
    setInterval(pollFullStatus, 10000);

    // ============================================================
    // Risk Limits Modal
    // ============================================================

    var RISK_SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD", "ADA/USD", "DOGE/USD"];

    window.openRiskModal = function () {
        var modal = document.getElementById("risk-modal");
        var body = document.getElementById("risk-body");
        modal.classList.add("active");
        body.innerHTML = '<span style="color:var(--text-muted)">Loading...</span>';

        fetch("/api/risk-limits")
            .then(function (r) { return r.json(); })
            .then(function (limits) { body.innerHTML = renderRiskForm(limits); })
            .catch(function (e) {
                body.innerHTML = '<span style="color:var(--red)">Failed to load risk limits: ' + escapeHtml(e.message) + '</span>';
            });
    };

    window.closeRiskModal = function () {
        document.getElementById("risk-modal").classList.remove("active");
    };

    document.getElementById("risk-modal").addEventListener("click", function (e) {
        if (e.target === document.getElementById("risk-modal")) closeRiskModal();
    });

    function renderRiskForm(limits) {
        var h = "";
        var qtyMap = limits.max_order_qty || {};
        var posMap = limits.max_position_qty || {};

        // Order Qty Limits
        h += '<div class="risk-section">';
        h += '<div class="risk-section-title">Max Order Quantity (per symbol)</div>';
        RISK_SYMBOLS.forEach(function (sym) {
            var key = sym.replace("/", "-");
            h += '<div class="risk-input-row">';
            h += '<label>' + escapeHtml(sym) + '</label>';
            h += '<input type="number" step="any" min="0" id="risk-qty-' + key + '" value="' + (qtyMap[sym] != null ? qtyMap[sym] : "") + '">';
            h += '</div>';
        });
        h += '</div>';

        // Notional Limit
        h += '<div class="risk-section">';
        h += '<div class="risk-section-title">Max Order Notional (global, limit orders)</div>';
        h += '<div class="risk-input-row">';
        h += '<label>USD Value</label>';
        h += '<input type="number" step="any" min="0" id="risk-notional" value="' + (limits.max_order_notional != null ? limits.max_order_notional : "") + '">';
        h += '</div>';
        h += '</div>';

        // Position Limits
        h += '<div class="risk-section">';
        h += '<div class="risk-section-title">Max Position Size (per symbol)</div>';
        RISK_SYMBOLS.forEach(function (sym) {
            var key = sym.replace("/", "-");
            h += '<div class="risk-input-row">';
            h += '<label>' + escapeHtml(sym) + '</label>';
            h += '<input type="number" step="any" min="0" id="risk-pos-' + key + '" value="' + (posMap[sym] != null ? posMap[sym] : "") + '">';
            h += '</div>';
        });
        h += '</div>';

        // Open Orders Limit
        h += '<div class="risk-section">';
        h += '<div class="risk-section-title">Max Open Orders (global)</div>';
        h += '<div class="risk-input-row">';
        h += '<label>Count</label>';
        h += '<input type="number" step="1" min="1" id="risk-open-orders" value="' + (limits.max_open_orders != null ? limits.max_open_orders : "") + '">';
        h += '</div>';
        h += '</div>';

        return h;
    }

    window.saveRiskLimits = function () {
        var limits = {
            max_order_qty: {},
            max_order_notional: parseFloat(document.getElementById("risk-notional").value) || 0,
            max_position_qty: {},
            max_open_orders: parseInt(document.getElementById("risk-open-orders").value, 10) || 0,
        };

        RISK_SYMBOLS.forEach(function (sym) {
            var key = sym.replace("/", "-");
            var qtyVal = parseFloat(document.getElementById("risk-qty-" + key).value);
            if (!isNaN(qtyVal) && qtyVal > 0) limits.max_order_qty[sym] = qtyVal;
            var posVal = parseFloat(document.getElementById("risk-pos-" + key).value);
            if (!isNaN(posVal) && posVal > 0) limits.max_position_qty[sym] = posVal;
        });

        fetch("/api/risk-limits", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(limits),
        })
            .then(function (r) { return r.json(); })
            .then(function (resp) {
                if (resp.status === "ok") {
                    showToast("Risk limits saved", "success");
                    closeRiskModal();
                } else {
                    showToast("Failed to save: " + (resp.message || "unknown error"), "error");
                }
            })
            .catch(function (e) {
                showToast("Failed to save risk limits: " + e.message, "error");
            });
    };

    // ============================================================
    // Troubleshoot Modal
    // ============================================================

    window.openTroubleshootModal = function () {
        var modal = document.getElementById("troubleshoot-modal");
        modal.classList.add("active");
        var input = document.getElementById("troubleshoot-input");
        input.value = "";
        document.getElementById("troubleshoot-response").textContent = "";
        document.getElementById("troubleshoot-send").disabled = false;
        setTimeout(function () { input.focus(); }, 50);
    };

    window.closeTroubleshootModal = function () {
        document.getElementById("troubleshoot-modal").classList.remove("active");
    };

    document.getElementById("troubleshoot-modal").addEventListener("click", function (e) {
        if (e.target === document.getElementById("troubleshoot-modal")) closeTroubleshootModal();
    });

    document.getElementById("troubleshoot-input").addEventListener("keydown", function (e) {
        if (e.ctrlKey && e.key === "Enter") {
            e.preventDefault();
            sendTroubleshoot();
        }
    });

    window.sendTroubleshoot = function () {
        var input = document.getElementById("troubleshoot-input");
        var responseEl = document.getElementById("troubleshoot-response");
        var sendBtn = document.getElementById("troubleshoot-send");
        var question = input.value.trim();

        if (!question) {
            showToast("Enter a question", "error");
            return;
        }

        sendBtn.disabled = true;
        responseEl.textContent = "";

        fetch("/api/troubleshoot", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ question: question }),
        }).then(function (resp) {
            if (!resp.ok && resp.headers.get("Content-Type").indexOf("application/json") >= 0) {
                return resp.json().then(function (err) { throw new Error(err.error || "Request failed"); });
            }
            var reader = resp.body.getReader();
            var decoder = new TextDecoder();
            var buffer = "";

            function read() {
                reader.read().then(function (result) {
                    if (result.done) {
                        sendBtn.disabled = false;
                        return;
                    }
                    buffer += decoder.decode(result.value, { stream: true });
                    var lines = buffer.split("\n");
                    buffer = lines.pop();

                    lines.forEach(function (line) {
                        if (line.indexOf("data: ") === 0) {
                            try {
                                var data = JSON.parse(line.substring(6));
                                if (data.text) {
                                    responseEl.textContent += data.text;
                                    responseEl.scrollTop = responseEl.scrollHeight;
                                }
                                if (data.done) {
                                    sendBtn.disabled = false;
                                }
                                if (data.error) {
                                    responseEl.textContent += "\n[Error: " + data.error + "]";
                                    sendBtn.disabled = false;
                                }
                            } catch (e) { /* ignore parse errors in stream */ }
                        }
                    });
                    read();
                });
            }
            read();
        }).catch(function (e) {
            responseEl.textContent = "Error: " + e.message;
            sendBtn.disabled = false;
        });
    };

    // ============================================================
    // Records Modal
    // ============================================================

    var _recordsData = [];

    window.openRecordsModal = function () {
        var modal = document.getElementById("records-modal");
        modal.classList.add("active");
        document.getElementById("records-detail").style.display = "none";
        document.getElementById("records-body").style.display = "";
        loadRecords();
    };

    window.closeRecordsModal = function () {
        document.getElementById("records-modal").classList.remove("active");
    };

    document.getElementById("records-modal").addEventListener("click", function (e) {
        if (e.target === document.getElementById("records-modal")) closeRecordsModal();
    });

    window.loadRecords = function () {
        var body = document.getElementById("records-body");
        var limit = document.getElementById("records-limit").value;
        body.innerHTML = '<span style="color:var(--text-muted)">Loading...</span>';
        document.getElementById("records-detail").style.display = "none";
        body.style.display = "";

        fetch("/api/records?limit=" + encodeURIComponent(limit))
            .then(function (r) { return r.json(); })
            .then(function (rows) {
                if (rows.error) {
                    body.innerHTML = '<span style="color:var(--red)">' + escapeHtml(rows.error) + '</span>';
                    return;
                }
                _recordsData = rows;
                renderRecords();
            })
            .catch(function (e) {
                body.innerHTML = '<span style="color:var(--red)">Failed to load records: ' + escapeHtml(e.message) + '</span>';
            });
    };

    function renderRecords() {
        var body = document.getElementById("records-body");
        var compFilter = document.getElementById("records-component").value;
        var dirFilter = document.getElementById("records-direction").value;
        var searchFilter = document.getElementById("records-search").value.toLowerCase();

        var filtered = _recordsData.filter(function (r) {
            if (compFilter && r.component !== compFilter) return false;
            if (dirFilter && r.direction !== dirFilter) return false;
            if (searchFilter && r.description.toLowerCase().indexOf(searchFilter) === -1
                && r.peer.toLowerCase().indexOf(searchFilter) === -1) return false;
            return true;
        });

        // Reverse so oldest first (data comes newest-first from API)
        filtered = filtered.slice().reverse();

        document.getElementById("records-count").textContent = filtered.length + " of " + _recordsData.length + " messages";

        if (filtered.length === 0) {
            body.innerHTML = '<span style="color:var(--text-muted)">No messages match filters.</span>';
            return;
        }

        var h = '<table class="records-table"><thead><tr>';
        h += '<th>Time</th><th>Component</th><th>Dir</th><th>Peer</th><th>Description</th>';
        h += '</tr></thead><tbody>';
        filtered.forEach(function (r, i) {
            var ts = r.timestamp && r.timestamp.length >= 23 ? r.timestamp.substring(11, 23) : r.timestamp;
            var dirClass = r.direction === "RECV" ? "dir-recv" : "dir-send";
            h += '<tr onclick="showRecordDetail(' + _recordsData.indexOf(r) + ')">';
            h += '<td class="col-ts">' + escapeHtml(ts) + '</td>';
            h += '<td class="col-component">' + escapeHtml(r.component) + '</td>';
            h += '<td class="' + dirClass + '">' + escapeHtml(r.direction) + '</td>';
            h += '<td class="col-peer">' + escapeHtml(r.peer) + '</td>';
            h += '<td class="col-desc">' + escapeHtml(r.description) + '</td>';
            h += '</tr>';
        });
        h += '</tbody></table>';
        body.innerHTML = h;
    }

    window.showRecordDetail = function (idx) {
        var r = _recordsData[idx];
        if (!r) return;
        document.getElementById("records-body").style.display = "none";
        var detail = document.getElementById("records-detail");
        detail.style.display = "";
        var content = document.getElementById("records-detail-content");
        var info = "ID:        " + r.id + "\n"
            + "Timestamp: " + r.timestamp + "\n"
            + "Component: " + r.component + "\n"
            + "Direction: " + r.direction + "\n"
            + "Peer:      " + r.peer + "\n"
            + "Description: " + r.description + "\n"
            + "\n--- Raw Message ---\n";
        if (r.raw_message) {
            try {
                info += JSON.stringify(JSON.parse(r.raw_message), null, 2);
            } catch (e) {
                info += r.raw_message;
            }
        } else {
            info += "(none)";
        }
        content.textContent = info;
    };

    window.closeRecordDetail = function () {
        document.getElementById("records-detail").style.display = "none";
        document.getElementById("records-body").style.display = "";
    };

    // Re-filter when filter controls change
    document.getElementById("records-component").addEventListener("change", renderRecords);
    document.getElementById("records-direction").addEventListener("change", renderRecords);
    document.getElementById("records-search").addEventListener("input", renderRecords);

    // ============================================================
    // Keyboard Shortcuts
    // ============================================================

    document.addEventListener("keydown", function (e) {
        // Escape closes modals
        if (e.key === "Escape") {
            closeAmendModal();
            closeConfigModal();
            closeStatusModal();
            closeRiskModal();
            closeTroubleshootModal();
            closeRecordsModal();
        }
        // Ctrl+B = BUY, Ctrl+S = SELL (guard both symmetrically: allow in order form inputs, block in textarea/search)
        if (e.ctrlKey && !e.altKey) {
            var tag = document.activeElement.tagName;
            var isBlockedField = tag === "TEXTAREA" || (tag === "INPUT" && document.activeElement.id === "records-search");
            if ((e.key === "b" || e.key === "B") && !isBlockedField) {
                e.preventDefault();
                setSide("BUY");
            } else if ((e.key === "s" || e.key === "S") && !isBlockedField) {
                e.preventDefault();
                setSide("SELL");
            } else if (e.key === "Enter") {
                // Ctrl+Enter = submit order
                e.preventDefault();
                submitOrder();
            }
        }
    });

    // ============================================================
    // Initialization
    // ============================================================

    function rehydrateFromDB() {
        fetch("/api/records?limit=1000")
            .then(function (r) { return r.json(); })
            .then(function (rows) {
                if (!Array.isArray(rows) || rows.length === 0) return;
                // Rows arrive newest-first; reverse to replay chronologically
                rows.reverse();
                rows.forEach(function (r) {
                    if (!r.raw_message) return;
                    try {
                        var data = JSON.parse(r.raw_message);
                        if (data.type === "execution_report") {
                            onExecutionReport(data);
                        }
                    } catch (e) { /* skip unparseable */ }
                });
                console.log("[App] Rehydrated " + state.orders.size + " orders from database");
            })
            .catch(function (e) {
                console.warn("[App] Failed to rehydrate from DB:", e);
            });
    }

    function init() {
        initMarketDataRows();
        onOrdTypeChange();
        updateSpreadDisplay();

        // Rehydrate order blotter from message database
        rehydrateFromDB();

        // Connect WebSockets
        connectWS("MKTDATA", WS_URLS.MKTDATA, onMarketData);
        connectWS("GUIBROKER", WS_URLS.GUIBROKER, onBrokerMessage);
        connectWS("POSMANAGER", WS_URLS.POSMANAGER, onPositionUpdate);

        console.log("[App] Crypto Trading Terminal initialized");
    }

    init();

})();
