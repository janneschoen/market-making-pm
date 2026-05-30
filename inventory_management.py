from market_action import get_token_balance, place_order, get_open_orders
from market_info import get_market_bids
from config import YES, NO
import asyncio

async def position_is_neutral(client, token_pair):
    number_of_yes = await get_token_balance(client, token_pair[YES])
    number_of_no = await get_token_balance(client, token_pair[NO])

    return (abs(number_of_yes - number_of_no) < MIN_ORDER_SIZE), (number_of_yes, number_of_no)


async def neutralise_positions(client, market: Market):
    token_pair = market.token_pair

    open_orders = await get_open_orders(client, market)
    for order in open_orders:
        await cancel_order(client, order)

    is_neutral, shares = await position_is_neutral(client, token_pair)

    while not is_neutral:
        number_of_yes, number_of_no = shares

        market_bids = await get_market_bids(token_pair)

        if number_of_yes > number_of_no:
            await place_order(
                client = client,
                token = token_pair[YES],
                price = market_bids[YES],
                size = number_of_yes - number_of_no,
                side = "SELL",
                isFOK = True
            )
        else:
            await place_order(
                client = client,
                token = token_pair[NO],
                price = market_bids[NO],
                size = number_of_no - number_of_yes,
                side = "SELL",
                isFOK = True
            )
        
        await asyncio.sleep(5)
        is_neutral, shares = await position_is_neutral(client, market["token_pair"])