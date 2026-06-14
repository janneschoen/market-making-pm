"""
Inventory risk management — force positions back to flat.

When a market exits the quoting cycle, any accumulated directional exposure
must be liquidated.  This module sells the excess tokens at the current
market bid (Fill-or-Kill) until YES ≈ NO (within minimal_order_size tolerance).

⚠ NOTE (from original README): Polymarket splitting/merging (redeeming
   complementary tokens for USDC) is NOT yet implemented.
"""
from market_action import get_token_balance, place_order, get_open_orders, cancel_order
from market_info import get_market_bids
from config import YES, NO, Config
from market_info import Market
import asyncio

async def position_is_neutral(run, client, token_pair):
    """
    Check whether net exposure is within the noise threshold.

    Returns (is_neutral: bool, (yes_balance, no_balance)).
    Neutral when |YES − NO| < minimal_order_size — i.e. the imbalance
    is smaller than the minimum tradeable size.
    """
    number_of_yes = await get_token_balance(client, token_pair[YES])
    number_of_no = await get_token_balance(client, token_pair[NO])

    return (abs(number_of_yes - number_of_no) < run.minimal_order_size), (number_of_yes, number_of_no)


async def neutralise_positions(run: Config, client, market: Market):
    """
    Cancel all open orders, then aggressively sell the excess token at
    the current market bid (FOK) until the position is flat.

    Strategy:
      - If long YES  (YES > NO) → sell YES at best YES bid.
      - If short YES (NO > YES) → sell NO  at best NO bid.

    Waits 5 s between attempts in case the book moves or fills fail.
    """
    token_pair = market.token_pair

    open_orders = await get_open_orders(client, market)
    for order in open_orders:
        await cancel_order(client, order)

    is_neutral, shares = await position_is_neutral(run, client, token_pair)

    max_attempts = 20  # ~100 s total with 5 s sleep; bail if still not flat
    attempt = 0
    while not is_neutral and attempt < max_attempts:
        number_of_yes, number_of_no = shares

        market_bids = await get_market_bids(token_pair)

        if number_of_yes > number_of_no:
            await place_order(
                run = run,
                client = client,
                token = token_pair[YES],
                price = market_bids[YES],
                size = number_of_yes - number_of_no,
                side = "SELL",
                is_FOK = True
            )
        else:
            await place_order(
                run = run,
                client = client,
                token = token_pair[NO],
                price = market_bids[NO],
                size = number_of_no - number_of_yes,
                side = "SELL",
                is_FOK = True
            )
        
        attempt += 1
        await asyncio.sleep(5)
        is_neutral, shares = await position_is_neutral(run, client, token_pair)
