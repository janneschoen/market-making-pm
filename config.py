"""
Bot configuration via JSON file.

Key parameters:
  trading_window    — [max_hours, min_hours] before resolution to trade
  number_of_markets — how many markets to quote simultaneously
  spread            — half-spread around own-mid (total width = spread)
  skew_intensity    — how aggressively to tilt quotes away from inventory
  refresh_rate      — seconds between quote updates
"""
import json
from dataclasses import dataclass, field
from typing import List

DEFAULT_CONFIG_PATH = "config.json"
# Binary-outcome token indices for a Polymarket CLOB pair
YES = 0
NO = 1

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

def load_config(config_path = DEFAULT_CONFIG_PATH):
    """
    Load trading parameters from a JSON config file.
    Returns a populated Config dataclass instance.
    """
    print(f"Config: '{config_path}'")
    try:
        with open(config_path, "r") as file:
            config = json.load(file)
    except Exception as e:
        print(f"Failed to load config from {config_path}: {e}")
    
    run = Config()

    # City names used to discover weather markets via slug
    run.locations = config["locations"]
    # How many top-scored markets to quote in parallel
    run.number_of_markets = config["number_of_markets"]
    # [max_hours, min_hours] before resolution — only trade inside this band
    run.trading_window = config["trading_window"]

    # Seconds between quote refresh cycles
    run.refresh_rate = config["refresh_rate"]

    # Orders smaller than this are skipped to avoid dust
    run.minimal_order_size = config["minimal_order_size"]
    # USDC notional size per quote (both YES and NO sides)
    run.standard_order_size = config["standard_order_size"]

    # Half-spread around own-mid; total quoted spread = spread
    run.spread = config["spread"]
    # Per-unit-of-exposure shift to own_mid; higher = faster inventory mean-reversion
    run.skew_intensity = config["skew_intensity"]

    # ── Simulation / paper-trading mode ──
    run.mode = config.get("mode", "live")
    sim = config.get("simulation", {})
    run.simulation_enabled = sim.get("enabled", run.mode != "live")
    run.simulation_initial_equity = float(sim.get("initial_equity", 100.0))
    run.simulation_log_path = sim.get("log_path", "logs/simulation.csv")
    run.simulation_metrics_interval_s = int(sim.get("metrics_interval_s", 300))
    run.simulation_equity_snapshot_interval_s = int(sim.get("equity_snapshot_interval_s", 60))

    return run