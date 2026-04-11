from invMan import initRelayClient, split, merge
from quotingCycle import doQuotingCycle
from exitLoop import neutralise
from marketInfo import getPrice, getOrderBook, getLocationMarkets, getHoursToRes
from marketAction import initClient, placeOrder, isNeutral
from config import LOCATIONS, NEUTRAL_NUM, TRADING_WINDOW, NUM_MARKETS
import time, json, asyncio


client = initClient()
relayClient = initRelayClient()

async def handleMarket(market):
    errorHeading = f"Error on market: '{market['question']}'"
    tokenPair = market["tokenPair"]

    print("Checking if neutral...")
    try:
        if await isNeutral(client, tokenPair):
            print("Already neutral.")
        else:
            print("Creating hedged portfolio...")
            await split(relayClient, market)
            if not await isNeutral(client, tokenPair):
                raise ValueError("No neutral portfolio after attempted split")
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
    
    print("Handled market successfully:", market['question'])
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
        print(f"- {market['question']} (vol: {volF}) (unc: {uncF}) (res: {hoursToRes}h)")

    tasks = []
    for market in tradingMarkets:
        tasks.append(asyncio.create_task(handleMarket(market)))  
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
