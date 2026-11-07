"""Risk limits: notional, position, daily loss and the kill switch."""
import pytest

from engine.risk import RiskLimits, RiskManager


def test_disabled_limits_allow_everything():
    risk = RiskManager(RiskLimits())
    assert not risk.enabled
    assert risk.check_order(price=0.9, size=10_000, position=10_000).allowed


def test_max_notional_accumulates_with_fills():
    risk = RiskManager(RiskLimits(max_notional=100.0))
    assert risk.check_order(price=0.5, size=100, position=0).allowed

    risk.record_fill(0.5, 100)          # 50 USDC committed
    assert risk.check_order(price=0.5, size=100, position=0).allowed

    risk.record_fill(0.5, 10)           # 55 USDC committed
    decision = risk.check_order(price=0.5, size=100, position=0)
    assert not decision.allowed
    assert "notional" in decision.reason


def test_max_position_per_market():
    risk = RiskManager(RiskLimits(max_position_per_market=10.0))
    assert risk.check_order(price=0.5, size=8, position=2).allowed
    decision = risk.check_order(price=0.5, size=8, position=4)
    assert not decision.allowed
    assert "position" in decision.reason


def test_daily_loss_limit_latches_the_kill_switch():
    risk = RiskManager(RiskLimits(max_daily_loss=5.0))
    risk.record_pnl(-4.0)
    assert risk.check_order(price=0.5, size=1, position=0).allowed

    risk.record_pnl(-5.0)
    assert risk.killed
    decision = risk.check_order(price=0.5, size=1, position=0)
    assert not decision.allowed
    assert "kill switch" in decision.reason
    assert "daily loss" in risk.halt_reason

    risk.resume()
    assert risk.check_order(price=0.5, size=1, position=0).allowed


def test_kill_switch_requires_explicit_resume():
    risk = RiskManager(RiskLimits(max_notional=1.0))
    risk.kill("manual")
    assert not risk.check_order(price=0.1, size=1, position=0).allowed
    risk.resume()
    assert risk.check_order(price=0.1, size=1, position=0).allowed


def test_roll_day_resets_counters():
    risk = RiskManager(RiskLimits(max_notional=100.0))
    risk.record_fill(0.5, 100)
    assert risk.notional_today == pytest.approx(50.0)

    risk.realized_pnl = -10.0
    risk.roll_day(risk.day)
    assert risk.notional_today == pytest.approx(50.0)   # same day: unchanged

    from datetime import timedelta
    risk.roll_day(risk.day + timedelta(days=1))
    assert risk.notional_today == pytest.approx(0.0)
    assert risk.realized_pnl == pytest.approx(0.0)
