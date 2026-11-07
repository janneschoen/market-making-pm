"""Attribution and mark-outs: the arithmetic behind the PnL claims."""
import pytest

from measure.attribution import (
    Trade,
    attribute,
    from_events,
    per_trade_edge,
    realized_spread,
)
from measure.markout import markout_cents, markout_series


def test_per_trade_edge_in_cents():
    assert per_trade_edge(0.62, 0.55, size=1) == pytest.approx(0.07)
    assert per_trade_edge(0.62, 0.55, fees=0.5, size=10) == pytest.approx(0.2)


def test_realized_spread_sign():
    assert realized_spread(0.48, 0.50, "BUY") == pytest.approx(0.02)
    assert realized_spread(0.52, 0.50, "SELL") == pytest.approx(0.02)
    assert realized_spread(0.52, 0.50, "BUY") == pytest.approx(-0.02)


def test_attribute_decomposes_edge_spread_and_fees():
    trades = [
        Trade(condition_id="c", token="YES", side="BUY", price=0.50,
              size=10, fees=0.10, mid_at_fill=0.52, model_p=0.60),
        Trade(condition_id="c", token="NO", side="SELL", price=0.55,
              size=10, fees=0.10, mid_at_fill=0.52),
    ]
    result = attribute(trades)

    assert result.trades == 2
    assert result.alpha == pytest.approx(1.0)       # (0.60 − 0.50) × 10
    assert result.spread == pytest.approx(0.5)      # 0.02×10 + 0.03×10
    assert result.fees == pytest.approx(0.2)
    assert result.net == pytest.approx(1.3)
    assert result.avg_edge_cents == pytest.approx(10.0)


def test_from_events_reads_fill_records():
    events = [
        {"event": "quote", "price": 0.4},
        {"event": "fill", "condition_id": "c", "token": "YES", "side": "BUY",
         "price": 0.48, "size": 5, "model_p": 0.55, "fees": 0},
        {"event": "fill", "condition_id": "c", "token": "NO"},
    ]
    trades = from_events(events)
    assert len(trades) == 1
    assert trades[0].price == pytest.approx(0.48)
    assert trades[0].model_p == pytest.approx(0.55)


def test_markout_sign_and_series():
    # Buying at 0.50 and the mid rising to 0.52 is 2¢ in our favour.
    assert markout_cents(0.50, 0.52, "BUY") == pytest.approx(2.0)
    assert markout_cents(0.50, 0.52, "SELL") == pytest.approx(-2.0)

    series = markout_series(0.50, "BUY", {"1m": 0.52, "5m": 0.48})
    assert [m.horizon for m in series] == ["1m", "5m"]
    assert series[0].cents == pytest.approx(2.0)
    assert series[1].cents == pytest.approx(-2.0)
