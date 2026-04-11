import json


CONFIG_PATH = "config.json"
try:
    with open(CONFIG_PATH, "r") as file:
        CONFIG = json.load(file)
except Exception as e:
    print(f"Failed to load config from {CONFIG_PATH}: {e}")

MIN_ORDER_SIZE = CONFIG["minOrderSize"]

LOCATIONS = CONFIG["locations"]
NUM_MARKETS = CONFIG["numMarkets"]
TRADING_WINDOW = CONFIG["tradingWindow"]

NEUTRAL_NUM = CONFIG["neutralShares"]
REFRESH_RATE = CONFIG["refreshRate"]
ORDER_SIZE = CONFIG["orderSize"]
MIN_SPREAD = CONFIG["minSpread"]
MAX_SPREAD = CONFIG["maxSpread"]
BEAT_SPREAD_BY = CONFIG["beatSpreadBy"]
SKEW_INTENSITY = CONFIG["skewIntensity"]

EXIT_BUFFER = CONFIG["exitBuffer"]
EXPOSURE_TOL = CONFIG["exposureTol"]
