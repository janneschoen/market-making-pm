import requests, json
from datetime import datetime, timezone
from config import YES, NO

async def get_price(id):
    resp = requests.get(f"https://clob.polymarket.com/price?token_id={id}&side=BUY").json()
    return float(resp["price"])

async def get_market_bids(token_pair):
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
    now = datetime.now(timezone.utc)
    hours_to_resolution = (market["resolution"] - now).total_seconds() / 3600
    return hours_to_resolution


def get_locations_markets(location, day_delay):
    now = datetime.now()
    month = now.strftime("%B").lower()
    day = now.day + dayDelay
    year = now.year

    slug = f"highest-temperature-in-{location.lower()}-on-{month}-{day}-{year}"
    url = f"https://gamma-api.polymarket.com/events/slug/{slug}"

    markets = requests.get(url).json()["markets"]

    locations_markets = [
        {
            "question": market["question"],
            "tokenPair": json.loads(market["clobTokenIds"]),
            "conditionId": market["conditionId"],
            "resolution": datetime.fromisoformat(market["endDate"]),
            "volume": float(market["volume"]),
            "uncertainty": 1.0 - 2 * abs(0.50 - float(json.loads(market["outcomePrices"])[0]))
        }
        for market in markets
    ]

    return locations_markets

