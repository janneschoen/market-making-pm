from config import NEUTRAL_NUM, EXIT_BUFFER, REFRESH_RATE, EXPOSURE_TOL
from marketAction import getTokenBalance, cancelOrders, placeOrder
from marketInfo import getOrderBook
from invMan import merge

async def neutralise(client, market):
    tokenPair = market["tokenPair"]
    inventory = await getTokenBalance(client, tokenPair[0])

    while not (NEUTRAL_NUM + EXPOSURE_TOL > inventory > NEUTRAL_NUM - EXPOSURE_TOL):
        await cancelOrders(client, tokenPair[0])
        surplus = inventory - NEUTRAL_NUM

        print("Surplus:", surplus)
        bids, asks = await getOrderBook(tokenPair[0])

        if exposure > 0:
            bestAsk = float(asks[-1]["price"]) if len(asks) else 0.99
            price = bestAsk - EXIT_BUFFER
            print("Sell order for", price)
            await placeOrder(client, tokenPair[0], price, surplus, "SELL", True)
        else:
            bestBid = float(bids[-1]["price"]) if len(bids) else 0.01
            price = bestBid + EXIT_BUFFER
            print("Buy order for", price)
            await placeOrder(client, tokenPair[0], price, -surplus, "BUY", True)

        await asyncio.sleep(REFRESH_RATE)
        inventory = await getTokenBalance(client, tokenPair[0])

    await cancelOrders(client, tokenPair[0])
    inventory = await getTokenBalance(client, tokenPair[0])
    await merge(relayClient, inventory, market)