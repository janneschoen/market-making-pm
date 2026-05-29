import json


CONFIG_PATH = "config.json"
try:
    with open(CONFIG_PATH, "r") as file:
        CONFIG = json.load(file)
except Exception as e:
    print(f"Failed to load config from {CONFIG_PATH}: {e}")

YES = 0
NO = 1

LOCATIONS = CONFIG["locations"]
NUM_MARKETS = CONFIG["number_of_markets"]
TRADING_WINDOW = CONFIG["trading_window"]

REFRESH_RATE = CONFIG["refresh_rate"]

MIN_ORDER_SIZE = CONFIG["minimal_order_size"]
ORDER_SIZE = CONFIG["standard_order_size"]

SPREAD = CONFIG["spread"]
SKEW_INTENSITY = CONFIG["skew_intensity"]

EXIT_BUFFER = CONFIG["exit_buffer"]
