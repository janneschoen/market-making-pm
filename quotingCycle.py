from marketInfo import getHoursToRes, getOrderBook
from marketAction import cancelOrders, placeOrder
from config import TRADING_WINDOW, NEUTRAL_NUM, SKEW_INTENSITY, BEAT_SPREAD_BY, MAX_SPREAD, MIN_SPREAD, REFRESH_RATE

async def doQuotingCycle(client, market):
    tokenPair = market["tokenPair"]
    while getHoursToRes(market) > TRADING_WINDOW[1]:

        bids, asks = await getOrderBook(tokenPair[0])
        bestBid = float(bids[-1]["price"]) if len(bids) else 0.01
        bestAsk = float(asks[-1]["price"]) if len(asks) else 0.99

        midPoint = (bestBid + bestAsk) / 2
        spread = bestAsk - bestBid

        await cancelOrders(client, tokenPair[0])

        inventory = await getTokenBalance(client, tokenPair[0])
        inventory = 40
        exposure = (inventory - NEUTRAL_NUM) / NEUTRAL_NUM

        myMidPoint = midPoint - (exposure * SKEW_INTENSITY)

        mySpread = min(MAX_SPREAD, max(spread - BEAT_SPREAD_BY, MIN_SPREAD))

        quotes = [
            round(myMidPoint - mySpread / 2, 4),
            round(myMidPoint + mySpread / 2, 4)
        ]
        print("MARKET:", bestBid, bestAsk)
        print("ME:", quotes)

        #await placeOrder(client, tokenPair[0], quotes[0], ORDER_SIZE, "BUY", False)
        #await placeOrder(client, tokenPair[0], quotes[1], ORDER_SIZE, "SELL", False)

        await asyncio.sleep(REFRESH_RATE)
