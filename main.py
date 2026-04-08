from limits import getLimit
from marketInfo import getPrice, getOrderBook, getTokens
from marketAction import initClient, getTokenBalance, cancelOrders, placeOrder
import time, json, asyncio


with open("config.json", "r") as file:
    data = json.load(file)

LOCATIONS = data["locations"][:1]

BETS = "YES", "NO"
NEUTRAL_NUM = data["neutralShareNum"]
ORDER_SIZE = data["orderSize"]
REFRESH_RATE = data["refreshRate"]

SPREAD_FACTOR = data["spreadFactor"]
MIN_SPREAD = data["minSpread"]
MAX_SPREAD = data["maxSpread"]
SKEW_INTENSITY = data["skewIntensity"]

print(LOCATIONS)

client = initClient()

async def handleMarket(tokenPair):

    while True:

        bids, asks = await getOrderBook(tokenPair[0])

        bestBid = float(bids[-1]["price"]) if len(bids) else 0.01
        bestAsk = float(asks[-1]["price"]) if len(asks) else 0.99
        print("Market Bid/Ask:", bestBid, bestAsk)

        midPoint = (bestBid + bestAsk) / 2
        print("Market midpoint:", midPoint)
        spread = bestAsk - bestBid
        print("Market spread:", spread)

        mySpread = max(MIN_SPREAD, min(MAX_SPREAD, spread * SPREAD_FACTOR))
        print("Own spread:", mySpread)
                   
        await cancelOrders(client, tokenPair[0])
        inventory = [await getTokenBalance(client, token) for token in tokenPair]
        exposure = (inventory[0] - inventory[1]) / inventory[1]

        print("Inventory:", inventory)
        print("Overexposed" if exposure > 0 else "Underexposed")
        print("Exposure:", exposure)

        myMidPoint = midPoint - (exposure * SKEW_INTENSITY)
        print("My midpoint:", myMidPoint)

        quotes = [
            round(myMidPoint - mySpread / 2, 4),
            round(myMidPoint + mySpread / 2, 4)
        ]
        print("Unskewed quotes:", round(midPoint - mySpread/2, 4), round(midPoint + mySpread/2, 4))
        print("Quotes:", quotes)

        await asyncio.sleep(REFRESH_RATE)

async def main():
    tasks = []

    for location in LOCATIONS:
        tokens = getTokens(location)
        print(len(tokens), "markets for", location)
        for tokenPair in tokens:
            tasks.append(asyncio.create_task(handleMarket(tokenPair)))

    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())