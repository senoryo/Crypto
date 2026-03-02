"""Tests for exchconn/bybit_sim.py — submit/cancel/amend order lifecycle."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from exchconn.bybit_sim import BybitSimulator, BASE_PRICES, PRICE_JITTER_PCT
from shared.fix_protocol import (
    FIXMessage, Tag, ExecType, OrdStatus, Side, OrdType,
    new_order_single, cancel_request, cancel_replace_request,
)


@pytest.fixture
def sim():
    """Create a BybitSimulator with a mock report callback (no price jitter loop)."""
    simulator = BybitSimulator()
    simulator._report_callback = AsyncMock()
    return simulator


# -----------------------------------------------------------------------
# TestBybitOrderIdGeneration
# -----------------------------------------------------------------------

class TestBybitOrderIdGeneration:

    def test_prefix(self, sim):
        oid = sim._next_order_id()
        assert oid.startswith("BYB-")
        assert oid == "BYB-000001"

    def test_sequential(self, sim):
        id1 = sim._next_order_id()
        id2 = sim._next_order_id()
        assert id1 == "BYB-000001"
        assert id2 == "BYB-000002"


# -----------------------------------------------------------------------
# TestBybitGetCurrentPrice
# -----------------------------------------------------------------------

class TestBybitGetCurrentPrice:

    def test_known_symbol_positive(self, sim):
        price = sim._get_current_price("BTC/USD")
        assert price > 0
        assert price == BASE_PRICES["BTC/USD"]

    def test_unknown_symbol_zero(self, sim):
        price = sim._get_current_price("XRP/USD")
        assert price == 0.0


# -----------------------------------------------------------------------
# TestBybitSubmitOrder
# -----------------------------------------------------------------------

class TestBybitSubmitOrder:

    @pytest.mark.asyncio
    async def test_creates_order(self, sim):
        msg = new_order_single("C1", "BTC/USD", Side.Buy, 1.0, OrdType.Limit, 67000.0)
        await sim.submit_order(msg)
        assert len(sim._orders) == 1
        assert "C1" in sim._cl_to_order

    @pytest.mark.asyncio
    async def test_sends_new_ack(self, sim):
        msg = new_order_single("C1", "BTC/USD", Side.Buy, 1.0, OrdType.Limit, 67000.0)
        await sim.submit_order(msg)
        sim._report_callback.assert_called_once()
        report = sim._report_callback.call_args[0][0]
        assert report.get(Tag.ExecType) == ExecType.New
        assert report.get(Tag.OrdStatus) == OrdStatus.New
        assert report.get(Tag.ClOrdID) == "C1"

    @pytest.mark.asyncio
    async def test_market_order_schedules_fill_task(self, sim):
        msg = new_order_single("C1", "BTC/USD", Side.Buy, 1.0, OrdType.Market)
        await sim.submit_order(msg)
        order_id = sim._cl_to_order["C1"]
        assert order_id in sim._fill_tasks
        sim._fill_tasks[order_id].cancel()
        try:
            await sim._fill_tasks[order_id]
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_limit_order_no_immediate_fill_task(self, sim):
        msg = new_order_single("C1", "BTC/USD", Side.Buy, 1.0, OrdType.Limit, 50000.0)
        await sim.submit_order(msg)
        order_id = sim._cl_to_order["C1"]
        assert order_id not in sim._fill_tasks


# -----------------------------------------------------------------------
# TestBybitCancelOrder
# -----------------------------------------------------------------------

class TestBybitCancelOrder:

    @pytest.mark.asyncio
    async def test_cancel_active_sends_canceled(self, sim):
        msg = new_order_single("C1", "BTC/USD", Side.Buy, 1.0, OrdType.Limit, 67000.0)
        await sim.submit_order(msg)
        sim._report_callback.reset_mock()

        cancel = cancel_request("CXL-1", "C1", "BTC/USD", Side.Buy)
        await sim.cancel_order(cancel)
        report = sim._report_callback.call_args[0][0]
        assert report.get(Tag.ExecType) == ExecType.Canceled
        assert report.get(Tag.OrdStatus) == OrdStatus.Canceled

    @pytest.mark.asyncio
    async def test_cancel_unknown_sends_rejected(self, sim):
        cancel = cancel_request("CXL-1", "UNKNOWN", "BTC/USD", Side.Buy)
        await sim.cancel_order(cancel)
        report = sim._report_callback.call_args[0][0]
        assert report.get(Tag.ExecType) == ExecType.Rejected

    @pytest.mark.asyncio
    async def test_cancel_inactive_sends_rejected(self, sim):
        msg = new_order_single("C1", "BTC/USD", Side.Buy, 1.0, OrdType.Limit, 67000.0)
        await sim.submit_order(msg)
        cancel1 = cancel_request("CXL-1", "C1", "BTC/USD", Side.Buy)
        await sim.cancel_order(cancel1)
        sim._report_callback.reset_mock()
        cancel2 = cancel_request("CXL-2", "C1", "BTC/USD", Side.Buy)
        await sim.cancel_order(cancel2)
        report = sim._report_callback.call_args[0][0]
        assert report.get(Tag.ExecType) == ExecType.Rejected
        assert "not active" in report.get(Tag.Text).lower()


# -----------------------------------------------------------------------
# TestBybitAmendOrder
# -----------------------------------------------------------------------

class TestBybitAmendOrder:

    @pytest.mark.asyncio
    async def test_amend_updates_qty_price(self, sim):
        msg = new_order_single("C1", "BTC/USD", Side.Buy, 1.0, OrdType.Limit, 67000.0)
        await sim.submit_order(msg)
        sim._report_callback.reset_mock()

        amend = cancel_replace_request("AMD-1", "C1", "BTC/USD", Side.Buy, 2.0, 68000.0)
        await sim.amend_order(amend)
        report = sim._report_callback.call_args[0][0]
        assert report.get(Tag.ExecType) == ExecType.Replaced
        assert report.get(Tag.OrdStatus) == OrdStatus.Replaced

        order_id = sim._cl_to_order["C1"]
        order = sim._orders[order_id]
        assert order.total_qty == 2.0
        assert order.price == 68000.0

    @pytest.mark.asyncio
    async def test_amend_unknown_rejected(self, sim):
        amend = cancel_replace_request("AMD-1", "UNKNOWN", "BTC/USD", Side.Buy, 2.0, 68000.0)
        await sim.amend_order(amend)
        report = sim._report_callback.call_args[0][0]
        assert report.get(Tag.ExecType) == ExecType.Rejected

    @pytest.mark.asyncio
    async def test_amend_updates_cl_ord_id_mapping(self, sim):
        msg = new_order_single("C1", "BTC/USD", Side.Buy, 1.0, OrdType.Limit, 67000.0)
        await sim.submit_order(msg)

        amend = cancel_replace_request("AMD-1", "C1", "BTC/USD", Side.Buy, 2.0, 68000.0)
        await sim.amend_order(amend)
        assert sim._cl_to_order["AMD-1"] == sim._cl_to_order["C1"]

    @pytest.mark.asyncio
    async def test_amend_report_includes_qty_and_price(self, sim):
        msg = new_order_single("C1", "BTC/USD", Side.Buy, 1.0, OrdType.Limit, 67000.0)
        await sim.submit_order(msg)
        sim._report_callback.reset_mock()

        amend = cancel_replace_request("AMD-1", "C1", "BTC/USD", Side.Buy, 3.0, 69000.0)
        await sim.amend_order(amend)
        report = sim._report_callback.call_args[0][0]
        assert float(report.get(Tag.OrderQty)) == 3.0
        assert float(report.get(Tag.Price)) == 69000.0
