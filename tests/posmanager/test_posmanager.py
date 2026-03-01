"""Tests for posmanager.posmanager.PositionManager — fill processing with mocked WS."""

import json
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from posmanager.posmanager import PositionManager


@pytest.fixture
def pos_manager():
    """Create a PositionManager with mocked WS server and client."""
    with patch("posmanager.posmanager.WSServer") as MockServer, \
         patch("posmanager.posmanager.WSClient") as MockClient:
        mock_server = MagicMock()
        mock_server.broadcast = AsyncMock()
        mock_server.send_to = AsyncMock()
        mock_server.start = AsyncMock()
        mock_server.stop = AsyncMock()
        mock_server.on_message = MagicMock()
        mock_server.on_connect = MagicMock()
        mock_server.clients = set()
        MockServer.return_value = mock_server

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.listen = AsyncMock()
        mock_client.close = AsyncMock()
        mock_client.on_message = MagicMock()
        MockClient.return_value = mock_client

        pm = PositionManager()
    return pm


class TestPositionManagerFillProcessing:

    @pytest.mark.asyncio
    async def test_valid_fill_updates_position(self, pos_manager):
        data = {
            "type": "fill",
            "symbol": "BTC/USD",
            "side": "BUY",
            "qty": 1.0,
            "price": 67000.0,
            "cl_ord_id": "GUI-1",
            "order_id": "OM-000001",
        }
        await pos_manager._process_fill(data)
        pos = pos_manager.positions["BTC/USD"]
        assert pos.qty == 1.0
        assert pos.avg_cost == 67000.0

    @pytest.mark.asyncio
    async def test_missing_symbol_ignored(self, pos_manager):
        data = {"type": "fill", "symbol": "", "side": "BUY", "qty": 1.0, "price": 100.0}
        await pos_manager._process_fill(data)
        assert len(pos_manager.positions) == 0

    @pytest.mark.asyncio
    async def test_zero_qty_ignored(self, pos_manager):
        data = {"type": "fill", "symbol": "BTC/USD", "side": "BUY", "qty": 0, "price": 100.0}
        await pos_manager._process_fill(data)
        assert "BTC/USD" not in pos_manager.positions

    @pytest.mark.asyncio
    async def test_zero_price_ignored(self, pos_manager):
        data = {"type": "fill", "symbol": "BTC/USD", "side": "BUY", "qty": 1.0, "price": 0}
        await pos_manager._process_fill(data)
        assert "BTC/USD" not in pos_manager.positions

    @pytest.mark.asyncio
    async def test_multiple_fills_accumulate(self, pos_manager):
        fill1 = {"type": "fill", "symbol": "ETH/USD", "side": "BUY", "qty": 5.0, "price": 3400.0,
                  "cl_ord_id": "G1", "order_id": "O1"}
        fill2 = {"type": "fill", "symbol": "ETH/USD", "side": "BUY", "qty": 5.0, "price": 3600.0,
                  "cl_ord_id": "G2", "order_id": "O2"}
        await pos_manager._process_fill(fill1)
        await pos_manager._process_fill(fill2)
        pos = pos_manager.positions["ETH/USD"]
        assert pos.qty == 10.0
        assert pos.avg_cost == pytest.approx(3500.0)
