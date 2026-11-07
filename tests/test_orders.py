"""Order acknowledgement checks (the post-before-cancel guard)."""
from engine.orders import order_id, order_rested


def test_live_order_with_id_is_rested():
    assert order_rested({"success": True, "orderID": "0x123"})
    assert order_id({"success": True, "orderID": "0x123"}) == "0x123"
    assert order_id({"orderId": "abc"}) == "abc"


def test_rejected_order_is_not_rested():
    assert not order_rested({"success": False, "errorMsg": "not enough balance"})
    assert not order_rested({"success": False})
    assert not order_rested({"error": "tick size"})
    assert not order_rested(None)
    assert not order_rested({})


def test_matched_order_counts_as_rested():
    assert order_rested({"status": "matched"})
    assert order_rested({"status": "live"})
    assert order_rested({"transactionsHashes": ["0xdead"]})
