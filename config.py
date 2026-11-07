"""
Bot configuration via JSON file.

Key parameters:
  trading_window    — [max_hours, min_hours] before resolution to trade
  number_of_markets — how many markets to quote simultaneously
  spread            — total quoted width around own-mid
  skew_intensity    — how aggressively to tilt quotes away from inventory
  refresh_rate      — seconds between quote updates
  risk              — hard limits enforced by `engine.risk`
  events_log_path   — JSONL event log (measurement layer)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:  # pragma: no cover
    from engine.risk import RiskLimits

DEFAULT_CONFIG_PATH = "config.json"

# Binary-outcome token indices for a Polymarket CLOB pair
YES = 0
NO = 1


def _default_risk_limits():
    # Imported lazily so `config` stays importable without the engine package
    # initialised (and to avoid a config ↔ engine import cycle).
    from engine.risk import RiskLimits
    return RiskLimits()


@dataclass
class Config:
    locations: List[str] = field(default_factory=list)
    number_of_markets: int = 10
    trading_window: List[int] = field(default_factory=lambda: [36, 12])

    refresh_rate: int = 5
    standard_order_size: int = 20
    spread: float = 0.04
    skew_intensity: float = 0.01
    minimal_order_size: int = 5

    # Simulation / paper-trading mode
    mode: str = "live"
    simulation_enabled: bool = False
    simulation_initial_equity: float = 100.0
    simulation_log_path: str = "logs/simulation.csv"
    simulation_metrics_interval_s: int = 300
    simulation_equity_snapshot_interval_s: int = 60

    # Measurement + market discovery
    events_log_path: str = "logs/events.jsonl"
    cache_dir: str = "logs/cache/gamma"
    discovery_cache_ttl_s: float = 300.0

    # Hard risk limits (all disabled by default)
    risk_limits: "RiskLimits" = field(default_factory=_default_risk_limits)


def _optional_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)


def load_config(config_path: str = DEFAULT_CONFIG_PATH) -> Config:
    """
    Load trading parameters from a JSON config file.

    Missing optional keys fall back to `Config` defaults; a missing or
    malformed file is a hard error (the old loader printed and then crashed
    later with a `NameError`).
    """
    print(f"Config: '{config_path}'")
    try:
        with open(config_path, "r", encoding="utf-8") as file:
            raw = json.load(file)
    except (OSError, ValueError) as error:
        raise SystemExit(f"Failed to load config from {config_path}: {error}")

    run = Config()

    run.locations = list(raw.get("locations", run.locations))
    run.number_of_markets = int(raw.get("number_of_markets",
                                        run.number_of_markets))
    run.trading_window = list(raw.get("trading_window", run.trading_window))

    run.refresh_rate = int(raw.get("refresh_rate", run.refresh_rate))
    run.minimal_order_size = int(raw.get("minimal_order_size",
                                         run.minimal_order_size))
    run.standard_order_size = int(raw.get("standard_order_size",
                                          run.standard_order_size))
    run.spread = float(raw.get("spread", run.spread))
    run.skew_intensity = float(raw.get("skew_intensity", run.skew_intensity))

    # ── Simulation / paper-trading mode ──
    run.mode = raw.get("mode", run.mode)
    sim = raw.get("simulation") or {}
    run.simulation_enabled = bool(sim.get("enabled", run.mode != "live"))
    run.simulation_initial_equity = float(
        sim.get("initial_equity", run.simulation_initial_equity)
    )
    run.simulation_log_path = sim.get("log_path", run.simulation_log_path)
    run.simulation_metrics_interval_s = int(
        sim.get("metrics_interval_s", run.simulation_metrics_interval_s)
    )
    run.simulation_equity_snapshot_interval_s = int(
        sim.get("equity_snapshot_interval_s",
                run.simulation_equity_snapshot_interval_s)
    )

    # ── Measurement / discovery ──
    run.events_log_path = raw.get("events_log_path", run.events_log_path)
    run.cache_dir = raw.get("cache_dir", run.cache_dir)
    run.discovery_cache_ttl_s = float(
        raw.get("discovery_cache_ttl_s", run.discovery_cache_ttl_s)
    )

    # ── Risk limits ──
    from engine.risk import RiskLimits
    risk = raw.get("risk") or {}
    run.risk_limits = RiskLimits(
        max_notional=_optional_float(risk.get("max_notional")),
        max_position_per_market=_optional_float(
            risk.get("max_position_per_market")
        ),
        max_daily_loss=_optional_float(risk.get("max_daily_loss")),
    )

    return run
