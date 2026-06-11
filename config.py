"""
Bot configuration via JSON file.

Key parameters:
  trading_window    — [max_hours, min_hours] before resolution to trade
  number_of_markets — how many markets to quote simultaneously
  spread            — half-spread around own-mid (total width = spread)
  skew_intensity    — how aggressively to tilt quotes away from inventory
  refresh_rate      — seconds between quote updates
  exit_buffer       — (unused) intended for early-exit margin before resolution
"""
import json
from dataclasses import dataclass
from typing import List

DEFAULT_CONFIG_PATH = "config.json"
# Binary-outcome token indices for a Polymarket CLOB pair
YES = 0
NO = 1

@dataclass
class Config:
    locations: List[str]
    number_of_markets: int
    trading_window: List[int]
    refresh_rate: int
    standard_order_size: int
    spread: float
    skew_intensity: float
    exit_buffer: float
    minimal_order_size: int = 5

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

    run.locations = config["locations"]
    run.number_of_markets = config["number_of_markets"]
    run.trading_window = config["trading_window"]

    run.refresh_rate = config["refresh_rate"]

    run.minimal_order_size = config["minimal_order_size"]
    run.standard_order_size = config["standard_order_size"]

    run.spread = config["spread"]
    run.skew_intensity = config["skew_intensity"]
    run.exit_buffer = config["exit_buffer"]

    return run