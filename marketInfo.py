import requests, json
from datetime import datetime

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

def getTokens(location):
    now = datetime.now()
    month = now.strftime("%B").lower()
    day = now.day + 1
    year = now.year

    slug = f"highest-temperature-in-{location.lower()}-on-{month}-{day}-{year}"
    url = f"https://gamma-api.polymarket.com/events/slug/{slug}"

    tokens = []
    markets = requests.get(url).json()["markets"]

    for market in markets:
        id_string = market["clobTokenIds"]
        tokenPair = json.loads(id_string)
        tokens.append(tokenPair)

    return tokens

