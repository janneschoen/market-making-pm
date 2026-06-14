"""
Entry point for the Polymarket market-making bot.

Workflow:
1. Scan configured locations for weather markets within the trading window
2. Score and rank markets by (volume × uncertainty) — a proxy for expected PnL
3. Run quoting cycles on the top-N markets in parallel via asyncio
4. Each market is neutralised (inventory flattened) before and after quoting
"""
from inventory_management import neutralise_positions
from quoting_cycle import do_quoting_cycle
from market_info import get_locations_markets, get_hours_to_resolution
from market_action import init_client
from config import load_config
import time, json, asyncio, sys

async def handle_market(run, client, market):
    """
    Lifecycle of a single market:
      1. Flatten any residual inventory (pre-cycle safety net)
      2. Run the continuous quoting loop until the trading window closes
      3. Flatten inventory again on exit (forces exit at market-bid)
    """
    error_heading = f"Error on market: '{market.question}'"

    # --- Stage 1: Pre-cycle inventory neutralisation ---
    # Ensures we start with a clean slate (no directional exposure carried
    # over from a previous run / partial fill).
    await neutralise_positions(run, client, market)
    
    print("Pre-cycle neutralisation complete.")

    print("Starting quoting cycle...")
    try:
        await do_quoting_cycle(run, client, market)
    except Exception as e:
        print(error_heading)
        print("Failed quoting cycle:", e)
        return

    # --- Stage 3: Post-cycle inventory neutralisation ---
    # Liquidate whatever inventory accumulated during quoting.
    # Sells at the current market bid → small slippage, but guarantees
    # the bot exits the market without directional risk.
    await neutralise_positions(run, client, market)
    
    print("Handled market successfully:", market.question)
    return


async def main():
    # --- Config & client bootstrap ---
    if len(sys.argv) > 1:
        run = load_config(sys.argv[1])
    else:
        run = load_config()

    client = init_client()
    all_markets = []

    # --- Market discovery ---
    # For each location, fetch weather markets. If today's market is outside
    # the trading window, walk forward day-by-day until we find one inside.
    MAX_DAY_DELAY = 30  # safety cap — markets more than 30 days out aren't listed
    for location in run.locations:
        day_delay = 0
        markets_of_location = get_locations_markets(location, day_delay)
        hours_to_resolution = await get_hours_to_resolution(markets_of_location[0])
    
        while day_delay < MAX_DAY_DELAY and not (run.trading_window[0] > hours_to_resolution > run.trading_window[1]):
            day_delay += 1
            markets_of_location = get_locations_markets(location, day_delay)
            hours_to_resolution = await get_hours_to_resolution(markets_of_location[0])
        
        if day_delay == MAX_DAY_DELAY:
            continue  # no market for this location in range, skip to next city
        
        for market in markets_of_location:
            all_markets.append(market)

    print(f"Scanned {len(all_markets)} markets for {len(run.locations)} locations.")

    # Second-pass filter: remove any market that moved outside the window
    # between discovery and now (edge case for boundary markets)
    filtered_markets = []
    for market in all_markets:
        hours_to_resolution = await get_hours_to_resolution(market)
        if run.trading_window[0] > hours_to_resolution > run.trading_window[1]:
            filtered_markets.append(market)
    all_markets = filtered_markets
    
    print(f"{len(all_markets)} markets matching filter.")

    # --- Market scoring & selection ---
    # Score = volume × uncertainty
    #   - volume: higher volume → tighter spreads, more fill probability
    #   - uncertainty: max at 50% (coin-flip), min at 0%/100% (resolved)
    # Sort descending, pick the top N.
    sorted_markets = sorted(all_markets, key=lambda market: market.trading_volume * market.uncertainty, reverse=True)

    trading_markets = sorted_markets[:run.number_of_markets]
    
    print(f"Retrieved best {len(trading_markets)} markets:")
    for market in trading_markets:
        print(
            f"- {market.question}",
            f"(vol.: {round(market.trading_volume, 2)})",
            f"(unc.: {round(market.uncertainty, 2)})",
            f"(res.: {round(await get_hours_to_resolution(market), 2)}h)"
        )
    
    # --- Parallel market handling ---
    # Each market runs independently: its own quoting loop + neutralisation.
    # asyncio.gather launches all coroutines concurrently.
    tasks = []
    for market in trading_markets:
        tasks.append(asyncio.create_task(handle_market(run, client, market)))  
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
