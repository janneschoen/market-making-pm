from market_info import get_hours_to_resolution, get_market_bids
from market_action import cancel_order, place_order, get_token_balance, get_open_orders
import asyncio

async def do_quoting_cycle(run: Config, client, market: Market):
    hours_to_resolution = await get_hours_to_resolution(market)
    while hours_to_resolution > run.trading_window[1]:

        best_yes_bid, best_no_bid = await get_market_bids(market.token_pair)

        market_mid = (best_yes_bid + best_no_bid) / 2
        # CORRECT MARKET MID CALC?
        
        market_spread = 1 - (best_yes_bid + best_no_bid)

        number_of_yes = await get_token_balance(client, market.token_pair[YES])
        number_of_no = await get_token_balance(client, market.token_pair[NO])

        bet_exposure = number_of_yes - number_of_no

        own_mid = market_mid - (bet_exposure * run.skew_intensity)

        # BID LOGIC ??

        previous_orders = await get_open_orders(client, market)

        for bet in YES, NO:
            await place_order(
                run = run,
                client = client,
                token = token_pair[bet],
                price = own_quotes[bet],
                size = run.standard_order_size,
                side = "BUY",
                is_FOK = False
            )

        for order in previous_orders:
            await cancel_order(client, order)

        print(f"\nMarket: {market.question}")
        print(f"Inventory: {number_of_yes} Y, {number_of_no} N ({round(bet_exposure, 2)})")

        print("──────────────────────────────────────────────────────────────")
        print(f"{'':<13} {'Yes':>10} {'No':>10} {'Spread':>10} {'Midpoint':>12}")
        print("──────────────────────────────────────────────────────────────")
        print(f"{'Market':<13} {best_yes_bid:>10.4f} {best_no_bid:>10.4f} {market_spread:>10.4f} {market_mid:>12.4f}")
        print(f"{'My Quotes':<13} {own_quotes[YES]:>10.4f} {own_quotes[NO]:>10.4f} {run.spread:>10.4f} {own_mid:>12.4f}")
        print("──────────────────────────────────────────────────────────────")

        await asyncio.sleep(run.refresh_rate)
        hours_to_resolution = await get_hours_to_resolution(market)
