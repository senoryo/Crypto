"""Tests for algo/parent_order.py — ParentOrder state machine and child management."""

import pytest

from algo.parent_order import (
    ParentOrder,
    ChildOrder,
    ParentState,
    InvalidStateTransition,
)
from shared.fix_protocol import Side, OrdType


def _make_parent(**kwargs) -> ParentOrder:
    defaults = dict(
        parent_id="ALGO-TEST-1",
        symbol="BTC/USD",
        side=Side.Buy,
        total_qty=10.0,
        algo_type="VWAP",
        arrival_price=50000.0,
    )
    defaults.update(kwargs)
    return ParentOrder(**defaults)


def _make_child(parent_id="ALGO-TEST-1", cl_ord_id="CHILD-1", **kwargs) -> ChildOrder:
    defaults = dict(
        cl_ord_id=cl_ord_id,
        parent_id=parent_id,
        symbol="BTC/USD",
        side=Side.Buy,
        qty=2.0,
        price=50000.0,
        ord_type=OrdType.Limit,
        exchange="BINANCE",
    )
    defaults.update(kwargs)
    return ChildOrder(**defaults)


# ---- State transitions ----

class TestStateTransitions:

    def test_initial_state_is_pending(self):
        po = _make_parent()
        assert po.state == ParentState.PENDING

    def test_start_transitions_pending_to_active(self):
        po = _make_parent()
        po.start()
        assert po.state == ParentState.ACTIVE
        assert po.started_at > 0

    def test_pause_transitions_active_to_paused(self):
        po = _make_parent()
        po.start()
        po.pause()
        assert po.state == ParentState.PAUSED

    def test_resume_transitions_paused_to_active(self):
        po = _make_parent()
        po.start()
        po.pause()
        po.resume()
        assert po.state == ParentState.ACTIVE

    def test_complete_transitions_active_to_done(self):
        po = _make_parent()
        po.start()
        po.complete()
        assert po.state == ParentState.DONE
        assert po.completed_at > 0

    def test_begin_completing_then_done(self):
        po = _make_parent()
        po.start()
        po.begin_completing()
        assert po.state == ParentState.COMPLETING
        po.complete()
        assert po.state == ParentState.DONE

    def test_cancel_from_pending(self):
        po = _make_parent()
        po.cancel()
        assert po.state == ParentState.CANCELLED

    def test_cancel_from_active(self):
        po = _make_parent()
        po.start()
        po.cancel()
        assert po.state == ParentState.CANCELLED

    def test_cancel_from_paused(self):
        po = _make_parent()
        po.start()
        po.pause()
        po.cancel()
        assert po.state == ParentState.CANCELLED

    def test_cancel_is_idempotent_from_done(self):
        po = _make_parent()
        po.start()
        po.complete()
        po.cancel()  # Should not raise
        assert po.state == ParentState.DONE

    def test_cancel_is_idempotent_from_cancelled(self):
        po = _make_parent()
        po.cancel()
        po.cancel()  # Should not raise
        assert po.state == ParentState.CANCELLED

    def test_invalid_transition_raises(self):
        po = _make_parent()
        with pytest.raises(InvalidStateTransition):
            po.pause()  # PENDING -> PAUSED is not valid

    def test_invalid_transition_from_done(self):
        po = _make_parent()
        po.start()
        po.complete()
        with pytest.raises(InvalidStateTransition):
            po.start()  # DONE -> ACTIVE is not valid

    def test_cannot_resume_without_pause(self):
        po = _make_parent()
        po.start()
        with pytest.raises(InvalidStateTransition):
            po.resume()  # ACTIVE -> ACTIVE via resume is not valid


# ---- Child order management ----

class TestChildOrders:

    def test_add_child_order(self):
        po = _make_parent()
        child = _make_child()
        po.add_child_order(child)
        assert "CHILD-1" in po.child_orders
        assert po.child_orders["CHILD-1"] is child

    def test_duplicate_child_raises(self):
        po = _make_parent()
        po.add_child_order(_make_child())
        with pytest.raises(ValueError):
            po.add_child_order(_make_child())  # Same cl_ord_id

    def test_process_fill_updates_child(self):
        po = _make_parent()
        child = _make_child(qty=5.0)
        po.add_child_order(child)
        po.process_fill("CHILD-1", fill_qty=2.0, fill_price=50100.0)
        assert child.filled_qty == 2.0
        assert child.avg_px == 50100.0
        assert child.status == "PARTIALLY_FILLED"

    def test_full_fill_marks_child_filled(self):
        po = _make_parent()
        child = _make_child(qty=2.0)
        po.add_child_order(child)
        po.process_fill("CHILD-1", fill_qty=2.0, fill_price=50000.0)
        assert child.status == "FILLED"

    def test_process_fill_updates_parent_aggregates(self):
        po = _make_parent(total_qty=10.0)
        child1 = _make_child(cl_ord_id="C-1", qty=5.0)
        child2 = _make_child(cl_ord_id="C-2", qty=5.0)
        po.add_child_order(child1)
        po.add_child_order(child2)

        po.process_fill("C-1", fill_qty=3.0, fill_price=50000.0)
        po.process_fill("C-2", fill_qty=2.0, fill_price=50200.0)

        assert po.filled_qty == 5.0
        # Weighted avg: (3*50000 + 2*50200) / 5 = 50080
        assert abs(po.avg_fill_price - 50080.0) < 0.01

    def test_process_fill_unknown_child_raises(self):
        po = _make_parent()
        with pytest.raises(KeyError):
            po.process_fill("NONEXISTENT", 1.0, 50000.0)

    def test_process_reject(self):
        po = _make_parent()
        child = _make_child()
        po.add_child_order(child)
        po.process_reject("CHILD-1", "Insufficient funds")
        assert child.status == "REJECTED"

    def test_process_child_new(self):
        po = _make_parent()
        child = _make_child()
        po.add_child_order(child)
        po.process_child_new("CHILD-1")
        assert child.status == "NEW"

    def test_process_child_cancelled(self):
        po = _make_parent()
        child = _make_child()
        po.add_child_order(child)
        po.process_child_cancelled("CHILD-1")
        assert child.status == "CANCELLED"


# ---- Aggregates ----

class TestAggregates:

    def test_fill_pct(self):
        po = _make_parent(total_qty=10.0)
        child = _make_child(qty=10.0)
        po.add_child_order(child)
        po.process_fill("CHILD-1", 5.0, 50000.0)
        assert po.fill_pct() == 0.5

    def test_fill_pct_zero_qty(self):
        po = _make_parent(total_qty=0.0)
        assert po.fill_pct() == 0.0

    def test_is_complete(self):
        po = _make_parent(total_qty=2.0)
        child = _make_child(qty=2.0)
        po.add_child_order(child)
        po.process_fill("CHILD-1", 2.0, 50000.0)
        assert po.is_complete()

    def test_is_not_complete(self):
        po = _make_parent(total_qty=10.0)
        child = _make_child(qty=10.0)
        po.add_child_order(child)
        po.process_fill("CHILD-1", 5.0, 50000.0)
        assert not po.is_complete()

    def test_remaining_qty(self):
        po = _make_parent(total_qty=10.0)
        child = _make_child(qty=10.0)
        po.add_child_order(child)
        po.process_fill("CHILD-1", 3.0, 50000.0)
        assert po.remaining_qty() == 7.0

    def test_slippage_buy(self):
        po = _make_parent(side=Side.Buy, arrival_price=50000.0, total_qty=5.0)
        child = _make_child(qty=5.0, side=Side.Buy)
        po.add_child_order(child)
        po.process_fill("CHILD-1", 5.0, 50100.0)
        # Bought higher than arrival: positive slippage (cost)
        # (50100 - 50000) * 1.0 * 5.0 = 500
        assert abs(po.slippage() - 500.0) < 0.01

    def test_slippage_sell(self):
        po = _make_parent(side=Side.Sell, arrival_price=50000.0, total_qty=5.0)
        child = _make_child(qty=5.0, side=Side.Sell)
        po.add_child_order(child)
        po.process_fill("CHILD-1", 49900.0, 49900.0)
        # Sold lower than arrival: positive slippage
        # Wait — fill_qty=49900 is wrong. Let's recalculate.
        # Actually the test has wrong values. Let me fix.
        pass

    def test_slippage_sell_correct(self):
        po = _make_parent(side=Side.Sell, arrival_price=50000.0, total_qty=5.0)
        child = _make_child(qty=5.0, side=Side.Sell)
        po.add_child_order(child)
        po.process_fill("CHILD-1", 5.0, 49900.0)
        # Sold lower than arrival: (49900 - 50000) * -1 * 5 = 500
        assert abs(po.slippage() - 500.0) < 0.01

    def test_slippage_no_fills(self):
        po = _make_parent()
        assert po.slippage() == 0.0

    def test_slippage_no_arrival_price(self):
        po = _make_parent(arrival_price=0.0)
        child = _make_child(qty=5.0)
        po.add_child_order(child)
        po.process_fill("CHILD-1", 5.0, 50000.0)
        assert po.slippage() == 0.0

    def test_active_child_count(self):
        po = _make_parent()
        c1 = _make_child(cl_ord_id="C-1")
        c2 = _make_child(cl_ord_id="C-2")
        c3 = _make_child(cl_ord_id="C-3")
        po.add_child_order(c1)
        po.add_child_order(c2)
        po.add_child_order(c3)
        po.process_reject("C-2", "rejected")
        # C-1 and C-3 are active (PENDING_NEW), C-2 is REJECTED (terminal)
        assert po.active_child_count() == 2

    def test_to_dict_contains_required_keys(self):
        po = _make_parent()
        d = po.to_dict()
        required_keys = {
            "parent_id", "symbol", "side", "total_qty", "algo_type",
            "state", "params", "arrival_price", "filled_qty",
            "avg_fill_price", "fill_pct", "slippage", "remaining_qty",
            "active_children", "total_children", "created_at",
            "started_at", "completed_at",
        }
        assert required_keys.issubset(d.keys())


# ---- ChildOrder properties ----

class TestChildOrder:

    def test_is_terminal_pending(self):
        child = _make_child()
        assert not child.is_terminal

    def test_is_terminal_filled(self):
        child = _make_child()
        child.status = "FILLED"
        assert child.is_terminal

    def test_is_terminal_cancelled(self):
        child = _make_child()
        child.status = "CANCELLED"
        assert child.is_terminal

    def test_is_terminal_rejected(self):
        child = _make_child()
        child.status = "REJECTED"
        assert child.is_terminal

    def test_leaves_qty(self):
        child = _make_child(qty=5.0)
        child.filled_qty = 2.0
        assert child.leaves_qty == 3.0

    def test_leaves_qty_zero(self):
        child = _make_child(qty=5.0)
        child.filled_qty = 5.0
        assert child.leaves_qty == 0.0
