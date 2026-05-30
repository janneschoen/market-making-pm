from market_info import get_hours_to_resolution, get_market_bids
from market_action import cancel_order, place_order, get_token_balance, get_open_orders
from datetime import datetime
from config import YES, NO
import asyncio

async def do_quoting_cycle(run: Config, client, market: Market):
    hours_to_resolution = await get_hours_to_resolution(market)
    while hours_to_resolution > run.trading_window[1]:

        yes_bid, no_bid = await get_market_bids(market.token_pair)

        yes_ask = 1 - no_bid # Complement of NO bid implies YES ask

        market_spread = yes_ask - yes_bid
        market_mid = (yes_bid + yes_ask) / 2

        # Calculate current exposure to bet outcome

        number_of_yes = await get_token_balance(client, market.token_pair[YES])
        number_of_no = await get_token_balance(client, market.token_pair[NO])

        bet_exposure = number_of_yes - number_of_no

        # Skew quotes linearly based on exposure
        own_mid = market_mid - (bet_exposure * run.skew_intensity)

        own_quotes = (None, None)

        # Buy YES at own midpoint - half spread
        own_quotes[YES] = own_mid - run.spread / 2

        # Instead of selling YES at own x = midpoint + half spread, buy NO at 1-x (equivalent)
        own_quotes[NO] = 1 - (own_mid + run.spread / 2)


        previous_orders = await get_open_orders(client, market)

        for bet in YES, NO:
            await place_order(
                run,
                client,
                token = token_pair[bet],
                price = own_quotes[bet],
                size = run.standard_order_size,
                side = "BUY", # YES and NO cancel each other => both buy orders
                is_FOK = False
            )

        # Cancel old orders after placing new ones to avoid order gap
        for order in previous_orders:
            await cancel_order(client, order)

        print(f"\nMarket: {market.question} | {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}")
        print(f"Inventory: {number_of_yes} Y, {number_of_no} N ({round(bet_exposure, 2)})")
        print("──────────────────────────────────────────────────────────────")
        print(f"{'':<13} {'Yes':>10} {'No':>10} {'Spread':>10} {'Midpoint':>12}")
        print("──────────────────────────────────────────────────────────────")
        print(f"{'Market':<13} {best_yes_bid:>10.4f} {best_no_bid:>10.4f} {market_spread:>10.4f} {market_mid:>12.4f}")
        print(f"{'My Quotes':<13} {own_quotes[YES]:>10.4f} {own_quotes[NO]:>10.4f} {run.spread:>10.4f} {own_mid:>12.4f}")
        print("──────────────────────────────────────────────────────────────")

        await asyncio.sleep(run.refresh_rate)
        hours_to_resolution = await get_hours_to_resolution(market)
