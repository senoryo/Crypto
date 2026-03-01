"""Tests for posmanager.posmanager.Position — position math, P&L, flips, edge cases."""

import pytest

from posmanager.posmanager import Position


# -----------------------------------------------------------------------
# TestPositionBuyFromFlat
# -----------------------------------------------------------------------

class TestPositionBuyFromFlat:

    def test_first_buy_sets_qty_and_cost(self):
        pos = Position("BTC/USD")
        pos.apply_fill("BUY", 1.0, 67000.0)
        assert pos.qty == 1.0
        assert pos.avg_cost == 67000.0

    def test_second_buy_averages(self):
        pos = Position("BTC/USD")
        pos.apply_fill("BUY", 1.0, 66000.0)
        pos.apply_fill("BUY", 1.0, 68000.0)
        assert pos.qty == 2.0
        assert pos.avg_cost == pytest.approx(67000.0)

    def test_unequal_sizes_weighted_average(self):
        pos = Position("BTC/USD")
        pos.apply_fill("BUY", 1.0, 60000.0)
        pos.apply_fill("BUY", 3.0, 68000.0)
        assert pos.qty == 4.0
        # (60000*1 + 68000*3) / 4 = 264000/4 = 66000
        assert pos.avg_cost == pytest.approx(66000.0)


# -----------------------------------------------------------------------
# TestPositionSellFromFlat
# -----------------------------------------------------------------------

class TestPositionSellFromFlat:

    def test_first_sell_creates_short(self):
        pos = Position("BTC/USD")
        pos.apply_fill("SELL", 2.0, 67000.0)
        assert pos.qty == -2.0
        assert pos.avg_cost == 67000.0

    def test_second_sell_averages(self):
        pos = Position("BTC/USD")
        pos.apply_fill("SELL", 1.0, 66000.0)
        pos.apply_fill("SELL", 1.0, 68000.0)
        assert pos.qty == -2.0
        assert pos.avg_cost == pytest.approx(67000.0)


# -----------------------------------------------------------------------
# TestPositionCloseLong
# -----------------------------------------------------------------------

class TestPositionCloseLong:

    def test_partial_close_profit(self):
        pos = Position("BTC/USD")
        pos.apply_fill("BUY", 2.0, 60000.0)
        pos.apply_fill("SELL", 1.0, 65000.0)
        assert pos.qty == 1.0
        assert pos.avg_cost == 60000.0  # avg_cost unchanged on partial close
        assert pos.realized_pnl == pytest.approx(5000.0)  # (65000 - 60000) * 1

    def test_full_close_loss(self):
        pos = Position("BTC/USD")
        pos.apply_fill("BUY", 1.0, 70000.0)
        pos.apply_fill("SELL", 1.0, 65000.0)
        assert pos.qty == 0.0
        assert pos.avg_cost == 0.0  # reset on full close
        assert pos.realized_pnl == pytest.approx(-5000.0)

    def test_full_close_exact_breakeven(self):
        pos = Position("BTC/USD")
        pos.apply_fill("BUY", 5.0, 3000.0)
        pos.apply_fill("SELL", 5.0, 3000.0)
        assert pos.qty == 0.0
        assert pos.realized_pnl == pytest.approx(0.0)


# -----------------------------------------------------------------------
# TestPositionCloseShort
# -----------------------------------------------------------------------

class TestPositionCloseShort:

    def test_partial_close_profit(self):
        pos = Position("BTC/USD")
        pos.apply_fill("SELL", 2.0, 70000.0)
        pos.apply_fill("BUY", 1.0, 65000.0)
        assert pos.qty == -1.0
        # Short profit: (avg_cost - fill_price) * close_qty = (70000-65000)*1 = 5000
        assert pos.realized_pnl == pytest.approx(5000.0)

    def test_full_close_loss(self):
        pos = Position("BTC/USD")
        pos.apply_fill("SELL", 1.0, 65000.0)
        pos.apply_fill("BUY", 1.0, 70000.0)
        assert pos.qty == 0.0
        assert pos.avg_cost == 0.0
        # Short loss: (65000 - 70000) * 1 = -5000
        assert pos.realized_pnl == pytest.approx(-5000.0)


# -----------------------------------------------------------------------
# TestPositionFlip
# -----------------------------------------------------------------------

class TestPositionFlip:

    def test_long_to_short(self):
        pos = Position("BTC/USD")
        pos.apply_fill("BUY", 1.0, 60000.0)
        # Sell 3: close 1 long, open 2 short
        pos.apply_fill("SELL", 3.0, 65000.0)
        assert pos.qty == -2.0
        assert pos.avg_cost == 65000.0  # new short avg cost
        # P&L from closing long: (65000 - 60000) * 1 = 5000
        assert pos.realized_pnl == pytest.approx(5000.0)

    def test_short_to_long(self):
        pos = Position("BTC/USD")
        pos.apply_fill("SELL", 2.0, 70000.0)
        # Buy 5: close 2 short, open 3 long
        pos.apply_fill("BUY", 5.0, 65000.0)
        assert pos.qty == 3.0
        assert pos.avg_cost == 65000.0  # new long avg cost
        # P&L from closing short: (70000 - 65000) * 2 = 10000
        assert pos.realized_pnl == pytest.approx(10000.0)


# -----------------------------------------------------------------------
# TestPositionUnrealizedPnL
# -----------------------------------------------------------------------

class TestPositionUnrealizedPnL:

    def test_long_profit(self):
        pos = Position("BTC/USD")
        pos.apply_fill("BUY", 2.0, 60000.0)
        pos.market_price = 65000.0
        # (65000 - 60000) * 2 = 10000
        assert pos.unrealized_pnl == pytest.approx(10000.0)

    def test_short_profit(self):
        pos = Position("BTC/USD")
        pos.apply_fill("SELL", 2.0, 70000.0)
        pos.market_price = 65000.0
        # (65000 - 70000) * (-2) = 10000
        assert pos.unrealized_pnl == pytest.approx(10000.0)

    def test_flat_returns_zero(self):
        pos = Position("BTC/USD")
        pos.market_price = 65000.0
        assert pos.unrealized_pnl == 0.0

    def test_no_market_price_returns_zero(self):
        pos = Position("BTC/USD")
        pos.apply_fill("BUY", 1.0, 60000.0)
        assert pos.market_price == 0.0
        assert pos.unrealized_pnl == 0.0


# -----------------------------------------------------------------------
# TestPositionToDict
# -----------------------------------------------------------------------

class TestPositionToDict:

    def test_all_fields_present(self):
        pos = Position("ETH/USD")
        pos.apply_fill("BUY", 10.0, 3400.0)
        pos.market_price = 3500.0
        d = pos.to_dict()
        assert d["symbol"] == "ETH/USD"
        assert d["qty"] == 10.0
        assert d["avg_cost"] == 3400.0
        assert d["market_price"] == 3500.0
        assert "unrealized_pnl" in d
        assert "realized_pnl" in d

    def test_rounding(self):
        pos = Position("BTC/USD")
        pos.apply_fill("BUY", 0.123456789, 67000.12345678)
        d = pos.to_dict()
        # qty rounded to 8, avg_cost rounded to 8
        assert d["qty"] == round(0.123456789, 8)
        assert d["avg_cost"] == round(67000.12345678, 8)


# -----------------------------------------------------------------------
# TestPositionEdgeCases
# -----------------------------------------------------------------------

class TestPositionEdgeCases:

    def test_zero_qty_ignored(self):
        pos = Position("BTC/USD")
        pos.apply_fill("BUY", 0.0, 67000.0)
        assert pos.qty == 0.0

    def test_negative_qty_ignored(self):
        pos = Position("BTC/USD")
        pos.apply_fill("BUY", -1.0, 67000.0)
        assert pos.qty == 0.0

    def test_unknown_side_ignored(self):
        pos = Position("BTC/USD")
        pos.apply_fill("HOLD", 1.0, 67000.0)
        assert pos.qty == 0.0
