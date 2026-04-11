from marketInfo import getHoursToRes, getOrderBook
from marketAction import cancelOrder, placeOrder, getTokenBalance, getOpenOrders
from config import *
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
        neutralShares = await getTokenBalance(client, tokenPair[1])
        toNeutralise = inventory - neutralShares

        exposure = toNeutralise / neutralShares

        myMidPoint = midPoint - (exposure * SKEW_INTENSITY)

        mySpread = min(MAX_SPREAD, max(spread - BEAT_SPREAD_BY, MIN_SPREAD))

        quotes = [
            round(myMidPoint - mySpread / 2, 4),
            round(myMidPoint + mySpread / 2, 4)
        ]

        buySize = ORDER_SIZE - (toNeutralise if exposure < 0 else 0)
        sellSize = ORDER_SIZE + (toNeutralise if exposure > 0 else 0)

        previousOrders = await getOpenOrders(client, market)

        await placeOrder(client, tokenPair[0], quotes[0], buySize, "BUY", False)
        await placeOrder(client, tokenPair[0], quotes[1], sellSize, "SELL", False)

        for order in previousOrders:
            await cancelOrder(client, order)

        print(f"\nInventory: {inventory} / {neutralShares} ({round(exposure,2)})")

        print("──────────────────────────────────────────────────────────────")
        print(f"{'':<13} {'Bid':>10} {'Ask':>10} {'Spread':>10} {'Midpoint':>12}")
        print("──────────────────────────────────────────────────────────────")
        print(f"{'Market':<13} {bestBid:>10.4f} {bestAsk:>10.4f} {spread:>10.4f} {midPoint:>12.4f}")
        print(f"{'My Quotes':<13} {quotes[0]:>10.4f} {quotes[1]:>10.4f} {mySpread:>10.4f} {myMidPoint:>12.4f}")
        print("──────────────────────────────────────────────────────────────")

        await asyncio.sleep(REFRESH_RATE)
        hoursToRes = await getHoursToRes(market)
