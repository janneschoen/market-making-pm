import json
from dataclasses import dataclass

DEFAULT_CONFIG_PATH = "config.json"
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
    exit_buffer: int
    minimal_order_size: int = 5

def load_config(config_path = DEFAULT_CONFIG_PATH):

    print(f"Config: '{config_path}'")
    try:
        with open(config_path, "r") as file:
            config = json.load(file)
    except Exception as e:
        print(f"Failed to load config from {config_path}: {e}")
    
    run = Config

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