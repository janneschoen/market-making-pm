from marketInfo import getHoursToRes, getOrderBook
from marketAction import cancelOrders, placeOrder, getTokenBalance
from config import TRADING_WINDOW, NEUTRAL_NUM, SKEW_INTENSITY, BEAT_SPREAD_BY, MAX_SPREAD, MIN_SPREAD, REFRESH_RATE, ORDER_SIZE
import asyncio

async def doQuotingCycle(client, market):
    tokenPair = market["tokenPair"]
    hoursToRes = await getHoursToRes(market)
    while hoursToRes > TRADING_WINDOW[1]:

        bids, asks = await getOrderBook(tokenPair[0])
        bestBid = float(bids[-1]["price"]) if len(bids) else 0.01
        bestAsk = float(asks[-1]["price"]) if len(asks) else 0.99

        midPoint = (bestBid + bestAsk) / 2
        spread = bestAsk - bestBid

        inventory = await getTokenBalance(client, tokenPair[0])
        toNeutralise = await getTokenBalance(client, tokenPair[1])
        exposure = (inventory - toNeutralise) / toNeutralise

        myMidPoint = midPoint - (exposure * SKEW_INTENSITY)

        mySpread = min(MAX_SPREAD, max(spread - BEAT_SPREAD_BY, MIN_SPREAD))

        quotes = [
            round(myMidPoint - mySpread / 2, 4),
            round(myMidPoint + mySpread / 2, 4)
        ]

        await cancelOrders(client, tokenPair[0])
        await placeOrder(client, tokenPair[0], quotes[0], ORDER_SIZE, "BUY", False)
        await placeOrder(client, tokenPair[0], quotes[1], ORDER_SIZE, "SELL", False)
    
        print(f"Inventory: {inventory} vs {toNeutralise} ({round(exposure,2)})")
        print(f"Market quotes: {bestBid, bestAsk} | Spread: {round(spread,4)} | Midpoint: {round(midPoint,4)} ")
        print(f"My Quotes: {quotes} | My Spread: {round(mySpread,4)} | My Midpoint: {round(myMidPoint,4)}")

        await asyncio.sleep(REFRESH_RATE)
        hoursToRes = await getHoursToRes(market)
