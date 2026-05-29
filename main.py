from inventory_management import neutralise_positions
from quoting_cycle import do_quoting_cycle
from market_info import get_locations_markets, get_hours_to_resolution
from market_action import init_client
from config import load_config
import time, json, asyncio, sys

async def handle_market(market):
    error_heading = f"Error on market: '{market.question}'"

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
    
    print("Handled market successfully:", market.question)
    return


async def main():
    if len(sys.argv) > 1:
        run = load_config(sys.argv[1])
    else:
        run = load_config()

    client = init_client()
    all_markets = []

    day_delay = 0
    for location in run.locations:
        markets_of_location = get_locations_markets(location, day_delay)
        hours_to_resolution = await get_hours_to_resolution(markets_of_location[0])
        while not (run.trading_window[0] > hours_to_resolution > run.trading_window[1]):
            day_delay += 1
            markets_of_location = get_locations_markets(location, day_delay)
            hours_to_resolution = await get_hours_to_resolution(markets_of_location[0])
        
        for market in markets_of_location:
            all_markets.append(market)

    print(f"Scanned {len(all_markets)} markets for {len(run.locations)} locations.")

    for market in all_markets:
        hours_to_resolution = await get_hours_to_resolution(market)
        if not (run.trading_window[0] > hours_to_resolution > run.trading_window[1]):
            all_markets.remove(market)
    
    print(f"{len(all_markets)} markets matching filter.")

    sorted_markets = sorted(all_markets, key=lambda market: market.trading_volume * market.uncertainty, reverse=True)

    trading_markets = sorted_markets[:run.number_of_markets]
    
    print(f"Retrieved best {len(trading_markets)} markets:")
    for market in trading_markets:
        print(
            f"- {market.question}",
            f"(vol: {round(market.trading_volume, 2)})",
            f"(unc: {round(market.uncertainty, 2)})",
            f"(res: {round(await get_hours_to_resolution(market), 2)}h)"
        )
    
    tasks = []
    for market in trading_markets:
        tasks.append(asyncio.create_task(handle_market(market)))  
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
