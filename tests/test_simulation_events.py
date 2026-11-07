"""Paper engine: fills are logged in the JSONL event stream with mark data."""
import pytest

from config import Config
from engine.markets import Market
from measure.logs import read_events
from simulation.engine import SimulationEngine


def make_run(tmp_path) -> Config:
    return Config(
        minimal_order_size=5,
        simulation_initial_equity=100.0,
        simulation_log_path=str(tmp_path / "simulation.csv"),
        events_log_path=str(tmp_path / "events.jsonl"),
    )


def make_market() -> Market:
    from datetime import datetime, timezone
    return Market(
        question="Q?",
        token_pair=["yes-token", "no-token"],
        condition_id="0xabc",
        resolution=datetime(2026, 6, 12, tzinfo=timezone.utc),
        trading_volume=1.0,
        uncertainty=1.0,
    )


def test_check_fills_writes_jsonl_fill_with_mid(tmp_path):
    run = make_run(tmp_path)
    market = make_market()
    engine = SimulationEngine(run)
    engine.register_market(market)

    engine.place_order(run, None, "yes-token", 0.55, 10, "BUY", False)
    fills = engine.check_fills(market, [0.50, 0.50], 24.0)

    assert len(fills) == 1
    # Our bid (0.55) crosses the live best bid (0.50) → paper fill.
    assert engine.inventory[market.condition_id][0] == pytest.approx(10.0)

    events = [event for event in read_events(run.events_log_path)
              if event["event"] == "fill"]
    assert len(events) == 1
    assert events[0]["mode"] == "paper"
    assert events[0]["condition_id"] == market.condition_id
    assert events[0]["token"] == "YES"
    assert events[0]["price"] == pytest.approx(0.55)
    assert events[0]["size"] == pytest.approx(10)
    # YES mid comes from `latest_mids` (0.5 after registration).
    assert events[0]["mid_at_fill"] == pytest.approx(0.5)


def test_no_fill_when_quote_is_behind_the_market(tmp_path):
    run = make_run(tmp_path)
    market = make_market()
    engine = SimulationEngine(run)
    engine.register_market(market)

    engine.place_order(run, None, "yes-token", 0.40, 10, "BUY", False)
    fills = engine.check_fills(market, [0.50, 0.50], 24.0)

    assert fills == []
    assert engine.total_fills_count == 0
    assert all(event["event"] != "fill"
               for event in read_events(run.events_log_path))
