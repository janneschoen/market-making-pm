import requests, json
from datetime import datetime, timezone

async def getPrice(id):
    resp = requests.get(f"https://clob.polymarket.com/price?token_id={id}&side=BUY").json()
    return float(resp["price"])

async def getOrderBook(token):
    url = "https://clob.polymarket.com/book"
    params = {"token_id": token}

    response = requests.get(url, params=params)
    data = response.json()

    bids = data.get("bids", [])
    asks = data.get("asks", [])

    return bids, asks

async def getHoursToRes(market):
    now = datetime.now(timezone.utc)
    hoursToRes = (market["resolution"] - now).total_seconds() / 3600
    return hoursToRes


def getLocationMarkets(location, dayDelay):
    now = datetime.now()
    month = now.strftime("%B").lower()
    day = now.day + dayDelay
    year = now.year

    slug = f"highest-temperature-in-{location.lower()}-on-{month}-{day}-{year}"
    url = f"https://gamma-api.polymarket.com/events/slug/{slug}"

    markets = requests.get(url).json()["markets"]

    locationMarkets = [
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

    return locationMarkets

