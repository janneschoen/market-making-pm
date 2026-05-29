from inventory_management import neutralise_positions
from quoting_cycle import do_quoting_cycle
from market_info import get_locations_markets, get_hours_to_resolution
from market_action import init_client
from config import LOCATIONS, TRADING_WINDOW, NUM_MARKETS
import time, json, asyncio

client = init_client()

async def handle_market(market):
    error_heading = f"Error on market: '{market['question']}'"
    token_pair = market["token_pair"]

    await neutralise_positions(client, market)
    
    print("Neutral.")

    print("Starting quoting cycle...")
    try:
        await do_quoting_cycle(client, market)
    except Exception as e:
        print(error_heading)
        print("Failed quoting cycle:", e)
        return

    await neutralise_positions(client, market)
    
    print("Handled market successfully:", market['question'])
    return


async def main():
    all_markets = []

    day_delay = 0
    for location in LOCATIONS:
        markets_of_location = get_locations_markets(location, day_delay)
        hours_to_resolution = await get_hours_to_resolution(markets_of_location[0])
        while not (TRADING_WINDOW[0] > hours_to_resolution > TRADING_WINDOW[1]):
            day_delay += 1
            markets_of_location = get_locations_markets(location, day_delay)
            hours_to_resolution = await get_hours_to_resolution(markets_of_location[0])
        
        for market in markets_of_location:
            all_markets.append(market)

    print(f"Scanned {len(all_markets)} markets for {len(LOCATIONS)} locations.")

    for market in all_markets:
        hours_to_resolution = await get_hours_to_resolution(market)
        if not (TRADING_WINDOW[0] > hours_to_resolution > TRADING_WINDOW[1]):
            all_markets.remove(market)
    
    print(f"{len(all_markets)} markets matching filter.")

    sorted_markets = sorted(all_markets, key=lambda market: market["volume"] * market["uncertainty"], reverse=True)

    trading_markets = sorted_markets[:NUM_MARKETS]
    print(f"Retrieved best {len(trading_markets)} markets:")
    for market in trading_markets:
        volume_f = round(market["volume"], 2)
        uncertainty_f = round(market["uncertainty"], 2)
        hours_to_resolution = round(await getHoursToRes(market), 2)
        print(f"- {market['question']} (vol: {volume_f}) (unc: {uncertainty_f}) (res: {hours_to_resolution}h)")

    tasks = []
    for market in trading_markets:
        tasks.append(asyncio.create_task(handle_market(market)))  
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
