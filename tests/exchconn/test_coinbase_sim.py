"""Tests for exchconn/coinbase_sim.py — CB-specific behavior."""

from unittest.mock import AsyncMock

import pytest

from exchconn.coinbase_sim import CoinbaseSimulator, BASE_PRICES, PRICE_JITTER_PCT
from shared.fix_protocol import (
    Tag, ExecType, OrdStatus, Side, OrdType,
    new_order_single, cancel_request,
)


@pytest.fixture
def cb_sim():
    """Create a CoinbaseSimulator with a mock report callback."""
    simulator = CoinbaseSimulator()
    simulator._report_callback = AsyncMock()
    return simulator


class TestCoinbaseSimulator:

    def test_prefix(self, cb_sim):
        oid = cb_sim._next_order_id()
        assert oid.startswith("CB-")
        assert oid == "CB-000001"

    def test_name_is_coinbase(self, cb_sim):
        assert cb_sim.name == "COINBASE"

    @pytest.mark.asyncio
    async def test_submit_sends_ack(self, cb_sim):
        msg = new_order_single("C1", "SOL/USD", Side.Buy, 10.0, OrdType.Limit, 178.0)
        await cb_sim.submit_order(msg)
        report = cb_sim._report_callback.call_args[0][0]
        assert report.get(Tag.ExecType) == ExecType.New
        assert report.get(Tag.OrdStatus) == OrdStatus.New

    @pytest.mark.asyncio
    async def test_cancel_works(self, cb_sim):
        msg = new_order_single("C1", "SOL/USD", Side.Buy, 10.0, OrdType.Limit, 178.0)
        await cb_sim.submit_order(msg)
        cb_sim._report_callback.reset_mock()

        cancel = cancel_request("CXL-1", "C1", "SOL/USD", Side.Buy)
        await cb_sim.cancel_order(cancel)
        report = cb_sim._report_callback.call_args[0][0]
        assert report.get(Tag.ExecType) == ExecType.Canceled

    def test_wider_price_jitter_than_binance(self):
        from exchconn.binance_sim import PRICE_JITTER_PCT as BIN_JITTER
        assert PRICE_JITTER_PCT > BIN_JITTER
