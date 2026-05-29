from market_info import get_hours_to_resolution, get_market_bids
from market_action import cancel_order, place_order, get_token_balance, get_open_orders
from config import *
import asyncio

async def do_quoting_cycle(client, market):
    token_pair = market["tokenPair"]
    hours_to_resolution = await get_hours_to_resolution(market)
    while hours_to_resolution > TRADING_WINDOW[1]:

        best_yes_bid, best_no_bid = await get_market_bids(token_pair)

        market_mid = (best_yes_bid + best_no_bid) / 2
        # CORRECT MARKET MID CALC?
        
        market_spread = 1 - (best_yes_bid + best_no_bid)

        number_of_yes = await get_token_balance(client, token_pair[YES])
        number_of_no = await get_token_balance(client, token_pair[NO])

        bet_exposure = number_of_yes - number_of_no

        own_mid = market_mid - (bet_exposure * SKEW_INTENSITY)

        # BID LOGIC ??

        previous_orders = await get_open_orders(client, market)

        for bet in YES, NO:
            await place_order(
                client = client,
                token = token_pair[bet],
                price = own_quotes[bet],
                size = ORDER_SIZE,
                side = "BUY",
                isFOK = False
            )

        for order in previous_orders:
            await cancel_order(client, order)

        print(f"\nMarket: {market["question"]}")
        print(f"Inventory: {number_of_yes} Y, {number_of_no} N ({round(bet_exposure, 2)})")

        print("──────────────────────────────────────────────────────────────")
        print(f"{'':<13} {'Yes':>10} {'No':>10} {'Spread':>10} {'Midpoint':>12}")
        print("──────────────────────────────────────────────────────────────")
        print(f"{'Market':<13} {best_yes_bid:>10.4f} {best_no_bid:>10.4f} {market_spread:>10.4f} {market_mid:>12.4f}")
        print(f"{'My Quotes':<13} {own_quotes[YES]:>10.4f} {own_quotes[NO]:>10.4f} {SPREAD:>10.4f} {own_mid:>12.4f}")
        print("──────────────────────────────────────────────────────────────")

        await asyncio.sleep(REFRESH_RATE)
        hours_to_resolution = await get_hours_to_resolution(market)
