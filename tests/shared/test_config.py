"""Tests for shared/config.py — port uniqueness, routing, ws_url()."""

from shared.config import (
    PORTS,
    SYMBOLS,
    EXCHANGES,
    DEFAULT_ROUTING,
    DEFAULT_RISK_LIMITS,
    HOST,
    ws_url,
)


class TestConfig:

    def test_all_component_ports_defined(self):
        expected = {"GUI_HTTP", "MKTDATA", "GUIBROKER", "OM", "EXCHCONN", "POSMANAGER"}
        assert expected.issubset(set(PORTS.keys()))

    def test_ports_unique(self):
        port_values = list(PORTS.values())
        assert len(port_values) == len(set(port_values)), "Duplicate ports found"

    def test_ws_url_format(self):
        url = ws_url("MKTDATA")
        assert url == f"ws://{HOST}:{PORTS['MKTDATA']}"
        assert url.startswith("ws://")

    def test_all_symbols_have_routing(self):
        for symbol in SYMBOLS:
            assert symbol in DEFAULT_ROUTING, f"{symbol} missing from DEFAULT_ROUTING"

    def test_routing_targets_are_valid_exchanges(self):
        for symbol, exchange in DEFAULT_ROUTING.items():
            assert exchange in EXCHANGES, f"Routing target {exchange} not in EXCHANGES"

    def test_default_risk_limits_has_all_sections(self):
        assert "max_order_qty" in DEFAULT_RISK_LIMITS
        assert "max_order_notional" in DEFAULT_RISK_LIMITS
        assert "max_position_qty" in DEFAULT_RISK_LIMITS
        assert "max_open_orders" in DEFAULT_RISK_LIMITS
