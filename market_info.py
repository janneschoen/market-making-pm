"""
Polymarket market-data helpers.

Fetch order-book bids, compute time-to-resolution, and scrape weather
markets from the Gamma API (Polymarket's market-discovery endpoint).
"""
import requests, json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List

async def get_price(id):
    """Get the midpoint price for a single token ID (BUY side)."""
    resp = requests.get(f"https://clob.polymarket.com/price?token_id={id}&side=BUY").json()
    return float(resp["price"])

async def get_market_bids(token_pair):
    """
    Return [best_yes_bid, best_no_bid] for a CLOB token pair.
    Uses the /book endpoint; falls back to 0.01 if the book is empty.
    """
    url = "https://clob.polymarket.com/book"

    best_market_bids = []

    for token in token_pair:
        params = {"token_id": token}
        response = requests.get(url, params=params)
        data = response.json()

        bids = data.get("bids", [])
        best_market_bids.append(float(bids[-1]["price"]) if len(bids) else 0.01)
    
    return best_market_bids

async def get_hours_to_resolution(market):
    """Hours remaining until the market resolves (UTC)."""
    now = datetime.now(timezone.utc)
    hours_to_resolution = (market.resolution - now).total_seconds() / 3600
    return hours_to_resolution

@dataclass
class Market:
    """Normalised representation of a single Polymarket binary market."""
    question: str          # e.g. "Highest temperature in London on June 12, 2026?"
    token_pair: List[str]  # [YES_token_id, NO_token_id] from clobTokenIds
    condition_id: str      # Polymarket condition ID (groups related markets)
    resolution: str        # UTC datetime of market resolution (endDate)
    trading_volume: float  # Total volume in USDC
    uncertainty: float     # 1 − 2|0.5 − p|  → 1.0 at 50%, 0.0 at 0%/100%

def get_locations_markets(location, day_delay):
    """
    Fetch all weather markets for a city on (today + day_delay).

    Polymarket weather markets follow the slug pattern:
        highest-temperature-in-{city}-on-{month}-{day}-{year}

    Returns a list of Market dataclass instances (typically 5–7 per location).
    """
    now = datetime.now()
    month = now.strftime("%B").lower()
    day = now.day + day_delay
    year = now.year

    slug = f"highest-temperature-in-{location.lower()}-on-{month}-{day}-{year}"
    url = f"https://gamma-api.polymarket.com/events/slug/{slug}"

    markets = requests.get(url).json()["markets"]

    # Normalise each raw market into our Market dataclass
    locations_markets = [
        Market(
            question = market["question"],
            token_pair = json.loads(market["clobTokenIds"]),
            condition_id = market["conditionId"],
            resolution = datetime.fromisoformat(market["endDate"]),
            trading_volume = float(market["volume"]),
            uncertainty = 1.0 - 2 * abs(0.50 - float(json.loads(market["outcomePrices"])[0])),
        )
        for market in markets
    ]

    return locations_markets

