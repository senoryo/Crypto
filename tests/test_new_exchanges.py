"""Tests for the 5 new exchange simulators (Kraken, Bybit, OKX, Bitfinex, HTX)
and their integration into ExchangeConnector."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from shared.fix_protocol import (
    FIXMessage, Tag, MsgType, ExecType, OrdStatus, Side, OrdType,
    new_order_single, cancel_request, cancel_replace_request, execution_report,
)
from exchconn.kraken_sim import KrakenSimulator
from exchconn.bybit_sim import BybitSimulator
from exchconn.okx_sim import OKXSimulator
from exchconn.bitfinex_sim import BitfinexSimulator
from exchconn.htx_sim import HTXSimulator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SIMULATORS = [
    (KrakenSimulator, "KRAKEN", "KRK"),
    (BybitSimulator, "BYBIT", "BYB"),
    (OKXSimulator, "OKX", "OKX"),
    (BitfinexSimulator, "BITFINEX", "BFX"),
    (HTXSimulator, "HTX", "HTX"),
]


def _make_sim(cls):
    """Create a simulator, wire up a report callback, and return both."""
    sim = cls()
    reports = []

    async def _capture(report):
        reports.append(report)

    sim.set_report_callback(_capture)
    return sim, reports


def _limit_order(cl_ord_id, exchange, symbol="BTC/USD", side=Side.Buy,
                 qty=1.0, price=67000.0):
    return new_order_single(
        cl_ord_id=cl_ord_id,
        symbol=symbol,
        side=side,
        qty=qty,
        ord_type=OrdType.Limit,
        price=price,
        exchange=exchange,
    )


def _market_order(cl_ord_id, exchange, symbol="BTC/USD", side=Side.Buy,
                  qty=1.0):
    return new_order_single(
        cl_ord_id=cl_ord_id,
        symbol=symbol,
        side=side,
        qty=qty,
        ord_type=OrdType.Market,
        exchange=exchange,
    )


# ===========================================================================
# Per-simulator parametrised tests (5 sims x 6 tests = 30 tests)
# ===========================================================================

@pytest.mark.parametrize("cls,name,prefix", SIMULATORS,
                         ids=[s[1] for s in SIMULATORS])
class TestSimulatorLifecycle:
    """Start/stop for each simulator."""

    @pytest.mark.asyncio
    async def test_start_stop(self, cls, name, prefix):
        sim, _ = _make_sim(cls)
        await sim.start()
        assert sim._running is True
        await sim.stop()
        assert sim._running is False

    @pytest.mark.asyncio
    async def test_name_and_prefix(self, cls, name, prefix):
        sim = cls()
        assert sim.name == name
        assert sim.prefix == prefix


@pytest.mark.parametrize("cls,name,prefix", SIMULATORS,
                         ids=[s[1] for s in SIMULATORS])
class TestSubmitOrder:
    """Submit order produces an ack with correct order-ID prefix."""

    @pytest.mark.asyncio
    async def test_submit_limit_order_ack(self, cls, name, prefix):
        sim, reports = _make_sim(cls)
        await sim.start()
        try:
            order = _limit_order("CL-1", name)
            await sim.submit_order(order)
            # Should have at least the New ack
            assert len(reports) >= 1
            ack = reports[0]
            assert ack.get(Tag.ExecType) == ExecType.New
            assert ack.get(Tag.OrdStatus) == OrdStatus.New
            assert ack.get(Tag.OrderID).startswith(f"{prefix}-")
        finally:
            await sim.stop()

    @pytest.mark.asyncio
    async def test_submit_market_order_ack(self, cls, name, prefix):
        sim, reports = _make_sim(cls)
        await sim.start()
        try:
            order = _market_order("CL-2", name)
            await sim.submit_order(order)
            assert len(reports) >= 1
            ack = reports[0]
            assert ack.get(Tag.ExecType) == ExecType.New
            assert ack.get(Tag.OrderID).startswith(f"{prefix}-")
        finally:
            await sim.stop()

    @pytest.mark.asyncio
    async def test_order_id_increments(self, cls, name, prefix):
        sim, reports = _make_sim(cls)
        await sim.start()
        try:
            await sim.submit_order(_limit_order("CL-A", name))
            await sim.submit_order(_limit_order("CL-B", name))
            ids = [r.get(Tag.OrderID) for r in reports
                   if r.get(Tag.ExecType) == ExecType.New]
            assert len(ids) == 2
            assert ids[0] != ids[1]
            # Both start with the correct prefix
            for oid in ids:
                assert oid.startswith(f"{prefix}-")
        finally:
            await sim.stop()


@pytest.mark.parametrize("cls,name,prefix", SIMULATORS,
                         ids=[s[1] for s in SIMULATORS])
class TestCancelOrder:
    """Cancel an active limit order."""

    @pytest.mark.asyncio
    async def test_cancel_active_order(self, cls, name, prefix):
        sim, reports = _make_sim(cls)
        await sim.start()
        try:
            await sim.submit_order(_limit_order("CL-1", name))
            ack = reports[0]
            order_id = ack.get(Tag.OrderID)

            cancel_msg = cancel_request(
                cl_ord_id="CL-CANCEL-1",
                orig_cl_ord_id="CL-1",
                symbol="BTC/USD",
                side=Side.Buy,
            )
            await sim.cancel_order(cancel_msg)

            cancel_reports = [r for r in reports
                              if r.get(Tag.ExecType) == ExecType.Canceled]
            assert len(cancel_reports) == 1
            cr = cancel_reports[0]
            assert cr.get(Tag.OrderID) == order_id
            assert cr.get(Tag.OrdStatus) == OrdStatus.Canceled
            assert float(cr.get(Tag.LeavesQty, "0")) == 0.0
        finally:
            await sim.stop()

    @pytest.mark.asyncio
    async def test_cancel_unknown_order_rejected(self, cls, name, prefix):
        sim, reports = _make_sim(cls)
        await sim.start()
        try:
            cancel_msg = cancel_request(
                cl_ord_id="CL-CANCEL-X",
                orig_cl_ord_id="CL-NONEXIST",
                symbol="BTC/USD",
                side=Side.Buy,
            )
            await sim.cancel_order(cancel_msg)

            assert len(reports) == 1
            assert reports[0].get(Tag.ExecType) == ExecType.Rejected
        finally:
            await sim.stop()


@pytest.mark.parametrize("cls,name,prefix", SIMULATORS,
                         ids=[s[1] for s in SIMULATORS])
class TestAmendOrder:
    """Amend (cancel/replace) an active limit order."""

    @pytest.mark.asyncio
    async def test_amend_active_order(self, cls, name, prefix):
        sim, reports = _make_sim(cls)
        await sim.start()
        try:
            await sim.submit_order(_limit_order("CL-1", name, price=67000.0, qty=5.0))
            ack = reports[0]
            order_id = ack.get(Tag.OrderID)

            amend_msg = cancel_replace_request(
                cl_ord_id="CL-AMEND-1",
                orig_cl_ord_id="CL-1",
                symbol="BTC/USD",
                side=Side.Buy,
                qty=10.0,
                price=68000.0,
            )
            await sim.amend_order(amend_msg)

            replaced = [r for r in reports
                        if r.get(Tag.ExecType) == ExecType.Replaced]
            assert len(replaced) == 1
            rp = replaced[0]
            assert rp.get(Tag.OrderID) == order_id
            assert rp.get(Tag.OrdStatus) == OrdStatus.Replaced
        finally:
            await sim.stop()

    @pytest.mark.asyncio
    async def test_amend_unknown_order_rejected(self, cls, name, prefix):
        sim, reports = _make_sim(cls)
        await sim.start()
        try:
            amend_msg = cancel_replace_request(
                cl_ord_id="CL-AMEND-X",
                orig_cl_ord_id="CL-NONEXIST",
                symbol="BTC/USD",
                side=Side.Buy,
                qty=2.0,
                price=65000.0,
            )
            await sim.amend_order(amend_msg)

            assert len(reports) == 1
            assert reports[0].get(Tag.ExecType) == ExecType.Rejected
        finally:
            await sim.stop()


# ===========================================================================
# ExchangeConnector registration tests
# ===========================================================================

class TestExchangeConnectorRegistration:
    """Verify all 5 new exchanges are registered in ExchangeConnector."""

    @pytest.mark.asyncio
    async def test_all_new_exchanges_registered(self):
        with patch("exchconn.exchconn.WSServer"):
            from exchconn.exchconn import ExchangeConnector
            ec = ExchangeConnector()
            for key in ("KRAKEN", "BYBIT", "OKX", "BITFINEX", "HTX"):
                assert key in ec._exchanges, f"{key} not registered"

    @pytest.mark.asyncio
    async def test_existing_exchanges_still_registered(self):
        with patch("exchconn.exchconn.WSServer"):
            from exchconn.exchconn import ExchangeConnector
            ec = ExchangeConnector()
            assert "BINANCE" in ec._exchanges
            assert "COINBASE" in ec._exchanges

    @pytest.mark.asyncio
    async def test_total_exchange_count(self):
        with patch("exchconn.exchconn.WSServer"):
            from exchconn.exchconn import ExchangeConnector
            ec = ExchangeConnector()
            assert len(ec._exchanges) == 7

    @pytest.mark.asyncio
    async def test_report_callbacks_set(self):
        with patch("exchconn.exchconn.WSServer"):
            from exchconn.exchconn import ExchangeConnector
            ec = ExchangeConnector()
            for key, exch in ec._exchanges.items():
                assert exch._report_callback is not None, \
                    f"{key} has no report callback"


# ===========================================================================
# Config EXCHANGES dict tests
# ===========================================================================

class TestConfigExchanges:
    """Verify shared/config.py EXCHANGES has correct entries."""

    def test_kraken_symbols(self):
        from shared.config import EXCHANGES
        assert "KRAKEN" in EXCHANGES
        syms = EXCHANGES["KRAKEN"]["symbols"]
        assert "BTC/USD" in syms
        assert "ETH/USD" in syms

    def test_bybit_symbols(self):
        from shared.config import EXCHANGES
        assert "BYBIT" in EXCHANGES
        syms = EXCHANGES["BYBIT"]["symbols"]
        assert syms["BTC/USD"] == "BTCUSDT"
        assert syms["ETH/USD"] == "ETHUSDT"

    def test_okx_symbols(self):
        from shared.config import EXCHANGES
        assert "OKX" in EXCHANGES
        syms = EXCHANGES["OKX"]["symbols"]
        assert syms["BTC/USD"] == "BTC-USDT"
        assert syms["ETH/USD"] == "ETH-USDT"

    def test_bitfinex_symbols(self):
        from shared.config import EXCHANGES
        assert "BITFINEX" in EXCHANGES
        syms = EXCHANGES["BITFINEX"]["symbols"]
        assert syms["BTC/USD"] == "tBTCUSD"
        assert syms["ETH/USD"] == "tETHUSD"

    def test_htx_symbols(self):
        from shared.config import EXCHANGES
        assert "HTX" in EXCHANGES
        syms = EXCHANGES["HTX"]["symbols"]
        assert syms["BTC/USD"] == "btcusdt"
        assert syms["ETH/USD"] == "ethusdt"

    def test_default_routing_has_all_symbols(self):
        from shared.config import DEFAULT_ROUTING
        assert "BTC/USD" in DEFAULT_ROUTING
        assert "ETH/USD" in DEFAULT_ROUTING
        assert "SOL/USD" in DEFAULT_ROUTING
        assert "ADA/USD" in DEFAULT_ROUTING
        assert "DOGE/USD" in DEFAULT_ROUTING
