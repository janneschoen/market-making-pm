from splitting import createSplit, initRelayClient
from marketInfo import getPrice, getOrderBook, getMarketInfo
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
relayClient = initRelayClient()

async def handleMarket(market):
    tokenPair = market["tokenPair"]

    for token in tokenPair:
        await cancelOrders(client, token)

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

        inventory = await getTokenBalance(client, tokenPair[0])
        exposure = (inventory - NEUTRAL_NUM) / NEUTRAL_NUM

        myMidPoint = midPoint - (exposure * SKEW_INTENSITY)

        print(f"Inventory: {inventory} (hedge: {NEUTRAL_NUM})")
        print("Overexposed" if exposure > 0 else "Underexposed")
        print("Relative exposure:", exposure)
        print("My midpoint:", myMidPoint)

        quotes = [
            round(myMidPoint - mySpread / 2, 4),
            round(myMidPoint + mySpread / 2, 4)
        ]
        print("Unskewed quotes:", round(midPoint - mySpread/2, 4), round(midPoint + mySpread/2, 4))
        print("Quotes:", quotes)

        await placeOrder(client, token[0], quotes[0], ORDER_SIZE, 0)
        await placeOrder(client, token[0], quotes[1], ORDER_SIZE, 1)

        await asyncio.sleep(REFRESH_RATE)

async def main():
    tasks = []

    for location in LOCATIONS:
        marketInfo = getMarketInfo(location)[:1]

        print(len(marketInfo), "markets for", location)

        for market in marketInfo:
            inventory = [await getTokenBalance(client, token) for token in market["tokenPair"]]
            if (inventory[0] or inventory[1]) and (inventory[0] != inventory[1]):
                print(f"{location} market {market["conditionId"]} currently unhedged.")
                print("Process will not continue until position is neutral.")
                exit()
            
            createSplit(relayClient, market["conditionId"], NEUTRAL_NUM)  

        for market in marketInfo:
            tasks.append(asyncio.create_task(handleMarket(market)))  

    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())