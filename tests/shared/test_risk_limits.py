"""Tests for shared/risk_limits.py — check_order() all 4 limit types + IO."""

import json
from unittest.mock import patch, mock_open

from shared.config import DEFAULT_RISK_LIMITS, SIDE_BUY, SIDE_SELL, ORD_TYPE_LIMIT, ORD_TYPE_MARKET
from shared.risk_limits import check_order, load_limits, save_limits


# -----------------------------------------------------------------------
# TestCheckOrderMaxQty
# -----------------------------------------------------------------------

class TestCheckOrderMaxQty:

    def test_within_limit(self, risk_limits):
        # Use market order to avoid notional check
        result = check_order(risk_limits, "BTC/USD", SIDE_BUY, 5.0, 0.0, ORD_TYPE_MARKET, {}, 0)
        assert result is None

    def test_at_limit(self, risk_limits):
        result = check_order(risk_limits, "BTC/USD", SIDE_BUY, 10.0, 0.0, ORD_TYPE_MARKET, {}, 0)
        assert result is None

    def test_exceeds_limit(self, risk_limits):
        result = check_order(risk_limits, "BTC/USD", SIDE_BUY, 11.0, 0.0, ORD_TYPE_MARKET, {}, 0)
        assert result is not None
        assert "exceeds max" in result

    def test_unknown_symbol_passes(self, risk_limits):
        # Unknown symbol has no qty limit and market order skips notional
        result = check_order(risk_limits, "XRP/USD", SIDE_BUY, 999999.0, 0.0, ORD_TYPE_MARKET, {}, 0)
        assert result is None


# -----------------------------------------------------------------------
# TestCheckOrderMaxNotional
# -----------------------------------------------------------------------

class TestCheckOrderMaxNotional:

    def test_within_limit(self, risk_limits):
        # 1 * 67000 = 67000, under 100000
        result = check_order(risk_limits, "BTC/USD", SIDE_BUY, 1.0, 67000.0, ORD_TYPE_LIMIT, {}, 0)
        assert result is None

    def test_exceeds_limit(self, risk_limits):
        # 2 * 67000 = 134000, over 100000
        result = check_order(risk_limits, "BTC/USD", SIDE_BUY, 2.0, 67000.0, ORD_TYPE_LIMIT, {}, 0)
        assert result is not None
        assert "notional" in result.lower()

    def test_market_order_skips_notional_check(self, risk_limits):
        # Market orders have no price, so notional check is skipped
        result = check_order(risk_limits, "BTC/USD", SIDE_BUY, 5.0, 0.0, ORD_TYPE_MARKET, {}, 0)
        assert result is None


# -----------------------------------------------------------------------
# TestCheckOrderMaxPosition
# -----------------------------------------------------------------------

class TestCheckOrderMaxPosition:

    def test_no_position_ok(self, risk_limits):
        result = check_order(risk_limits, "BTC/USD", SIDE_BUY, 5.0, 0.0, ORD_TYPE_MARKET, {}, 0)
        assert result is None

    def test_position_plus_order_exceeds(self, risk_limits):
        positions = {"BTC/USD": 45.0}
        result = check_order(risk_limits, "BTC/USD", SIDE_BUY, 6.0, 0.0, ORD_TYPE_MARKET, positions, 0)
        assert result is not None
        assert "position" in result.lower()

    def test_sell_reduces_position(self, risk_limits):
        positions = {"BTC/USD": 45.0}
        # Selling 5 brings position to 40, within max 50
        result = check_order(risk_limits, "BTC/USD", SIDE_SELL, 5.0, 3500.0, ORD_TYPE_LIMIT, positions, 0)
        assert result is None

    def test_sell_creates_short_exceeds(self, risk_limits):
        # ETH max_position_qty=500, max_order_qty=100. Sell 100 ETH at low price from flat.
        # Projected position = -100, within max_position_qty=500, so use higher qty.
        # Actually use SOL: max_order_qty=5000, max_position_qty=25000.
        # Selling 5000 SOL from flat: projected=-5000, abs(5000) < 25000 — passes.
        # For a clear position breach: sell enough to exceed max. BTC max_pos=50.
        # Sell 8 BTC (under qty max 10) from flat: projected=-8, abs(8) < 50 — still passes.
        # Need position that pushes over: current_pos=0, sell 100 ETH -> projected=-100 < max 500.
        # Let's use a custom limit set for clarity:
        custom = {"max_order_qty": {"BTC/USD": 100.0}, "max_position_qty": {"BTC/USD": 50.0}}
        result = check_order(custom, "BTC/USD", SIDE_SELL, 60.0, 0.0, ORD_TYPE_MARKET, {}, 0)
        assert result is not None
        assert "position" in result.lower()


# -----------------------------------------------------------------------
# TestCheckOrderMaxOpenOrders
# -----------------------------------------------------------------------

class TestCheckOrderMaxOpenOrders:

    def test_under_limit(self, risk_limits):
        result = check_order(risk_limits, "BTC/USD", SIDE_BUY, 1.0, 0.0, ORD_TYPE_MARKET, {}, 10)
        assert result is None

    def test_at_limit(self, risk_limits):
        result = check_order(risk_limits, "BTC/USD", SIDE_BUY, 1.0, 0.0, ORD_TYPE_MARKET, {}, 50)
        assert result is not None
        assert "open order" in result.lower()

    def test_above_limit(self, risk_limits):
        result = check_order(risk_limits, "BTC/USD", SIDE_BUY, 1.0, 0.0, ORD_TYPE_MARKET, {}, 55)
        assert result is not None


# -----------------------------------------------------------------------
# TestCheckOrderCombined
# -----------------------------------------------------------------------

class TestCheckOrderCombined:

    def test_all_defaults_pass(self, risk_limits):
        result = check_order(risk_limits, "ETH/USD", SIDE_BUY, 1.0, 3000.0, ORD_TYPE_LIMIT, {}, 0)
        assert result is None

    def test_empty_limits_pass_everything(self):
        empty_limits = {}
        result = check_order(empty_limits, "BTC/USD", SIDE_BUY, 99999.0, 99999.0, ORD_TYPE_LIMIT, {}, 999)
        assert result is None


# -----------------------------------------------------------------------
# TestLoadSaveLimits
# -----------------------------------------------------------------------

class TestLoadSaveLimits:

    @patch("shared.risk_limits.os.path.exists", return_value=False)
    def test_load_returns_defaults_when_file_missing(self, mock_exists):
        limits = load_limits()
        assert "max_order_qty" in limits
        assert "max_order_notional" in limits
        assert limits == DEFAULT_RISK_LIMITS

    @patch("shared.risk_limits.os.path.exists", return_value=True)
    def test_load_reads_json(self, mock_exists):
        custom = {"max_order_qty": {"BTC/USD": 999.0}, "max_open_orders": 100}
        with patch("builtins.open", mock_open(read_data=json.dumps(custom))):
            limits = load_limits()
        assert limits["max_open_orders"] == 100
        assert limits["max_order_qty"]["BTC/USD"] == 999.0

    def test_save_writes_json(self, tmp_path):
        filepath = tmp_path / "risk_limits.json"
        custom = {"max_open_orders": 25}
        with patch("shared.risk_limits.RISK_LIMITS_FILE", str(filepath)):
            save_limits(custom)
        with open(filepath) as f:
            data = json.load(f)
        assert data["max_open_orders"] == 25
