"""
Inventory risk management — force positions back to flat.

When a market exits the quoting cycle, any accumulated directional exposure
must be liquidated.  This module sells the excess tokens at the current
market bid (Fill-or-Kill) until YES ≈ NO (within minimal_order_size tolerance).

Paper mode (engine != None): neutralises paper inventory via SimulationEngine.

⚠ NOTE (from original README): Polymarket splitting/merging (redeeming
   complementary tokens for USDC) is NOT yet implemented.
"""
from market_action import get_token_balance, place_order, get_open_orders, cancel_order
from market_info import get_market_bids
from config import YES, NO, Config
from market_info import Market
from simulation.engine import SimulationEngine
import asyncio


async def _get_balance(run, client, token, engine):
    """Get token balance — from engine (paper) or CLOB (live)."""
    if engine is not None:
        return engine.get_balance(token)
    return await get_token_balance(client, token)


async def _get_open_orders(run, client, market, engine):
    if engine is not None:
        return engine.get_open_orders(market)
    return await get_open_orders(client, market)


async def _place_order(run, client, token, price, size, side, is_FOK, engine):
    if engine is not None:
        return engine.place_order(run, client, token, price, size, side, is_FOK)
    return await place_order(run, client, token, price, size, side, is_FOK)


async def _cancel_order(run, client, order_id, engine):
    if engine is not None:
        engine.cancel_order(order_id)
        return
    return await cancel_order(client, order_id)


async def position_is_neutral(run, client, token_pair, engine=None):
    """
    Check whether net exposure is within the noise threshold.

    Returns (is_neutral: bool, (yes_balance, no_balance)).
    Neutral when |YES − NO| < minimal_order_size — i.e. the imbalance
    is smaller than the minimum tradeable size.
    """
    number_of_yes = await _get_balance(run, client, token_pair[YES], engine)
    number_of_no = await _get_balance(run, client, token_pair[NO], engine)

    return (abs(number_of_yes - number_of_no) < run.minimal_order_size), (number_of_yes, number_of_no)


async def neutralise_positions(run: Config, client, market: Market,
                               engine: SimulationEngine = None):
    """
    Cancel all open orders, then aggressively sell the excess token at
    the current market bid (FOK) until the position is flat.

    Strategy:
      - If long YES  (YES > NO) → sell YES at best YES bid.
      - If short YES (NO > YES) → sell NO  at best NO bid.

    Waits 5 s between attempts in case the book moves or fills fail.
    """
    token_pair = market.token_pair

    open_orders = await _get_open_orders(run, client, market, engine)
    for order in open_orders:
        await _cancel_order(run, client, order, engine)

    is_neutral, shares = await position_is_neutral(run, client, token_pair, engine)

    max_attempts = 20  # ~100 s total with 5 s sleep; bail if still not flat
    attempt = 0
    while not is_neutral and attempt < max_attempts:
        number_of_yes, number_of_no = shares

        market_bids = await get_market_bids(token_pair)

        if number_of_yes > number_of_no:
            await _place_order(
                run = run,
                client = client,
                token = token_pair[YES],
                price = market_bids[YES],
                size = number_of_yes - number_of_no,
                side = "SELL",
                is_FOK = True,
                engine = engine,
            )
        else:
            await _place_order(
                run = run,
                client = client,
                token = token_pair[NO],
                price = market_bids[NO],
                size = number_of_no - number_of_yes,
                side = "SELL",
                is_FOK = True,
                engine = engine,
            )
        
        attempt += 1
        await asyncio.sleep(5)
        is_neutral, shares = await position_is_neutral(run, client, token_pair, engine)
