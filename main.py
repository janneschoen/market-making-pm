from invMan import initRelayClient, split, merge
from quotingCycle import doQuotingCycle
from exitLoop import neutralise
from marketInfo import getPrice, getOrderBook, getLocationMarkets, getHoursToRes
from marketAction import initClient, getTokenBalance, cancelOrders, placeOrder
from config import LOCATIONS, NEUTRAL_NUM, TRADING_WINDOW, NUM_MARKETS
import time, json, asyncio


client = initClient()
relayClient = initRelayClient()

async def handleMarket(market):
    errorHeading = f"Error on market: '{market["question"]}'"

    try:
        tokenPair = market["tokenPair"]
        for token in tokenPair:
            await cancelOrders(client, token)
    except Exception as e:
        print(errorHeading)
        print("Error:", e)
        return

    try:
        yesTokens = await getTokenBalance(client, tokenPair[0])
        noTokens = await getTokenBalance(client, tokenPair[1])
        if yesTokens == noTokens > 0:
            print("Already neutral:", yesTokens, noTokens)
        else:
            print("Creating hedged portfolio...")
            await split(relayClient, market)
    except Exception as e:
        print(errorHeading)
        print("Failed splitting:", e)
        return

    print("Starting quoting cycle...")
    try:
        await doQuotingCycle(client, market)
    except Exception as e:
        print(errorHeading)
        print("Failed quoting cycle:", e)
        return

    print("Neutralising portfolio...")
    try:
        await neutralise(relayClient, market)
    except Exception as e:
        print(errorHeading)
        print("Failed neutralising:", e)
        return
    
    print("Handled market successfully:", market["question"])
    return


async def main():
    allMarkets = []

    dayDelay = 0
    for location in LOCATIONS:
        locMarkets = getLocationMarkets(location, dayDelay)
        hoursToRes = await getHoursToRes(locMarkets[0])
        while not (TRADING_WINDOW[0] > hoursToRes > TRADING_WINDOW[1]):
            dayDelay += 1
            locMarkets = getLocationMarkets(location, dayDelay)
            hoursToRes = await getHoursToRes(locMarkets[0])
        
        for market in locMarkets:
            allMarkets.append(market)

    print(f"Scanned {len(allMarkets)} markets for {len(LOCATIONS)} locations.")

    for market in allMarkets:
        hoursToRes = await getHoursToRes(market)
        if not (TRADING_WINDOW[0] > hoursToRes > TRADING_WINDOW[1]):
            allMarkets.remove(market)
    
    print(f"{len(allMarkets)} markets matching filter.")

    sortedMarkets = sorted(allMarkets, key=lambda market: market["volume"] * market["uncertainty"], reverse=True)

    tradingMarkets = sortedMarkets[:NUM_MARKETS]
    print(f"Retrieved best {len(tradingMarkets)} markets:")
    for market in tradingMarkets:
        volF = round(market["volume"], 2)
        uncF = round(market["uncertainty"], 2)
        hoursToRes = round(await getHoursToRes(market), 2)
        print(f"- {market["question"]} (vol: {volF}) (unc: {uncF}) (res: {hoursToRes}h)")

    tasks = []
    for market in tradingMarkets:
        tasks.append(asyncio.create_task(handleMarket(market)))  
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())