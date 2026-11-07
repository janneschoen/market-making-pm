"""Quoting strategy: window enforcement, order verification, logging, risk."""
import asyncio
import json
from datetime import datetime, timezone

import pytest

import strategies.inventory_mm as mm
from config import Config
from engine.markets import Market
from engine.risk import RiskLimits
from measure.logs import read_events
from simulation.engine import SimulationEngine


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def make_config(tmp_path, window=(36, 12)) -> Config:
    return Config(
        locations=["London"],
        trading_window=list(window),
        refresh_rate=0,
        standard_order_size=10,
        minimal_order_size=5,
        spread=0.04,
        skew_intensity=0.0,
        simulation_initial_equity=100.0,
        simulation_log_path=str(tmp_path / "simulation.csv"),
        events_log_path=str(tmp_path / "events.jsonl"),
    )


def make_market() -> Market:
    return Market(
        question="Highest temperature in London on June 12?",
        token_pair=["yes-token", "no-token"],
        condition_id="0xabc",
        resolution=utc(2026, 6, 12),
        trading_volume=100.0,
        uncertainty=1.0,
    )


def quote_events(path):
    return [event for event in read_events(path) if event["event"] == "quote"]


def test_cycle_does_not_run_above_the_trading_window(monkeypatch, tmp_path):
    """The old loop only enforced the lower bound (ROADMAP §10)."""
    run = make_config(tmp_path, window=(36, 12))
    market = make_market()

    async def never_inside_window(_market, now=None):
        return 100.0        # above the 36 h upper bound

    async def bids_must_not_be_fetched(_token_pair):
        raise AssertionError("book fetched outside the trading window")

    monkeypatch.setattr(mm, "get_hours_to_resolution", never_inside_window)
    monkeypatch.setattr(mm, "get_market_bids", bids_must_not_be_fetched)

    engine = SimulationEngine(run)
    engine.register_market(market)
    asyncio.run(mm.do_quoting_cycle(run, None, market, engine))

    assert engine.total_orders_placed == 0
    assert quote_events(run.events_log_path) == []


def test_cycle_quotes_both_legs_and_logs_the_book(monkeypatch, tmp_path):
    run = make_config(tmp_path)
    market = make_market()
    hours = iter([24.0, 1.0])           # inside the window once, then out

    async def hours_to_resolution(_market, now=None):
        return next(hours)

    async def bids(_token_pair):
        return [0.48, 0.50]

    monkeypatch.setattr(mm, "get_hours_to_resolution", hours_to_resolution)
    monkeypatch.setattr(mm, "get_market_bids", bids)

    engine = SimulationEngine(run)
    engine.register_market(market)
    asyncio.run(mm.do_quoting_cycle(run, None, market, engine))

    assert engine.total_orders_placed == 2
    assert engine.latest_quotes[market.condition_id][0] == pytest.approx(0.47)
    assert engine.latest_quotes[market.condition_id][1] == pytest.approx(0.49)

    events = quote_events(run.events_log_path)
    assert len(events) == 1
    assert events[0]["yes_bid"] == pytest.approx(0.48)
    assert events[0]["market_mid"] == pytest.approx(0.49)
    assert events[0]["yes_quote"] == pytest.approx(0.47)
    assert events[0]["no_quote"] == pytest.approx(0.49)
    assert events[0]["exposure"] == pytest.approx(0.0)
    assert events[0]["mode"] == "paper"


def test_failed_leg_withdraws_new_quotes_and_keeps_previous(monkeypatch, tmp_path):
    """A rejected post must not leave the book empty (ROADMAP §10)."""
    run = make_config(tmp_path)
    market = make_market()
    hours = iter([24.0, 1.0])
    cancelled = []
    placed = []

    async def hours_to_resolution(_market, now=None):
        return next(hours)

    async def bids(_token_pair):
        return [0.48, 0.50]

    async def balance(_run, _client, _token, _engine):
        return 0.0

    async def open_orders(_run, _client, _market, _engine):
        return ["old-1"]

    async def place(_run, _client, token, price, size, side, is_FOK, engine=None):
        placed.append((token, price))
        if token == "no-token":
            return {"success": False, "errorMsg": "post-only cross"}
        return {"success": True, "orderID": "new-1"}

    async def cancel(_run, _client, order_id, _engine):
        cancelled.append(order_id)

    monkeypatch.setattr(mm, "get_hours_to_resolution", hours_to_resolution)
    monkeypatch.setattr(mm, "get_market_bids", bids)
    monkeypatch.setattr(mm, "_get_balance", balance)
    monkeypatch.setattr(mm, "_get_open_orders", open_orders)
    monkeypatch.setattr(mm, "_place_order", place)
    monkeypatch.setattr(mm, "_cancel_order", cancel)

    asyncio.run(mm.do_quoting_cycle(run, object(), market, engine=None))

    assert [token for token, _ in placed] == ["yes-token", "no-token"]
    assert cancelled == ["new-1"]        # new leg pulled, old set preserved
    assert "old-1" not in cancelled

    events = list(read_events(run.events_log_path))
    assert any(e["event"] == "quote_abandoned" for e in events)
    assert all(e.get("rested") is not False for e in events
               if e["event"] == "order" and e["token"] == "YES")


def test_risk_limits_block_orders(monkeypatch, tmp_path):
    run = make_config(tmp_path)
    run.risk_limits = RiskLimits(max_notional=1.0)
    market = make_market()
    hours = iter([24.0, 1.0])

    async def hours_to_resolution(_market, now=None):
        return next(hours)

    async def bids(_token_pair):
        return [0.48, 0.50]

    monkeypatch.setattr(mm, "get_hours_to_resolution", hours_to_resolution)
    monkeypatch.setattr(mm, "get_market_bids", bids)

    engine = SimulationEngine(run)
    engine.register_market(market)
    asyncio.run(mm.do_quoting_cycle(run, None, market, engine))

    assert engine.total_orders_placed == 0
    events = list(read_events(run.events_log_path))
    assert any(e["event"] == "risk_block" for e in events)


def test_event_log_is_valid_jsonl(tmp_path):
    run = make_config(tmp_path)
    market = make_market()
    from measure.logs import get_logger

    logger = get_logger(run.events_log_path)
    logger.quote(condition_id=market.condition_id, yes_bid=0.5)

    with open(run.events_log_path, "r", encoding="utf-8") as handle:
        lines = [json.loads(line) for line in handle if line.strip()]

    assert len(lines) == 1
    assert lines[0]["event"] == "quote"
    assert lines[0]["condition_id"] == market.condition_id
    assert "T" in lines[0]["ts"] and "+00:00" in lines[0]["ts"]
