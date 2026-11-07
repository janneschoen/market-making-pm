"""
Position management: balances and force-neutralisation.

When a market exits the quoting cycle, accumulated directional exposure is
liquidated by selling the excess token at the best bid (Fill-or-Kill) until
YES ≈ NO within `minimal_order_size`.

⚠ ROADMAP §8: splitting/merging YES+NO pairs for USDC is still **not**
implemented.  Selling a redeemable pair at the bid gives back the spread the
strategy just earned; this must be replaced before Phase 3 (live capital).
"""
from __future__ import annotations

import asyncio
from typing import Tuple

from config import NO, YES, Config
from engine import orders as clob
from engine.markets import Market, get_market_bids
from measure.logs import get_logger

NEUTRALISE_SLEEP_S = 5.0
MAX_NEUTRALISE_ATTEMPTS = 20
# Below ~1.5¢ a "bid" is the empty-book placeholder, not a real market.
MIN_SELLABLE_BID = 0.015


async def _get_balance(run, client, token, engine) -> float:
    """Token balance — from the paper engine or the live CLOB."""
    if engine is not None:
        return engine.get_balance(token)
    return await clob.get_token_balance(client, token)


async def _get_open_orders(run, client, market, engine):
    if engine is not None:
        return engine.get_open_orders(market)
    return await clob.get_open_orders(client, market)


async def _place_order(run, client, token, price, size, side, is_FOK, engine):
    if engine is not None:
        return engine.place_order(run, client, token, price, size, side, is_FOK)
    return await clob.place_order(run, client, token, price, size, side, is_FOK)


async def _cancel_order(run, client, order_id, engine):
    if engine is not None:
        engine.cancel_order(order_id)
        return None
    return await clob.cancel_order(client, order_id)


async def position_is_neutral(run, client, token_pair,
                              engine=None) -> Tuple[bool, Tuple[float, float]]:
    """
    Check whether net exposure is within the noise threshold.

    Returns `(is_neutral, (yes_balance, no_balance))`; neutral means
    `|YES − NO| < minimal_order_size` (imbalance below tradeable size).
    """
    number_of_yes = await _get_balance(run, client, token_pair[YES], engine)
    number_of_no = await _get_balance(run, client, token_pair[NO], engine)
    neutral = abs(number_of_yes - number_of_no) < run.minimal_order_size
    return neutral, (number_of_yes, number_of_no)


async def neutralise_positions(run: Config, client, market: Market,
                               engine=None) -> Tuple[float, float]:
    """
    Cancel open orders, then sell the excess token at the market bid (FOK)
    until the position is flat.  Returns the final `(yes, no)` balances.

    Long YES → sell YES; long NO → sell NO.  Sleeps between attempts in case
    the book moves or a FOK fails, and gives up after ~100 s.
    """
    log = get_logger(getattr(run, "events_log_path", "logs/events.jsonl"))
    token_pair = market.token_pair

    for order in await _get_open_orders(run, client, market, engine):
        await _cancel_order(run, client, order, engine)

    is_neutral, shares = await position_is_neutral(
        run, client, token_pair, engine
    )
    log.log(
        "neutralise_start",
        condition_id=market.condition_id,
        question=market.question,
        yes=shares[0],
        no=shares[1],
        neutral=is_neutral,
    )

    attempt = 0
    while not is_neutral and attempt < MAX_NEUTRALISE_ATTEMPTS:
        number_of_yes, number_of_no = shares
        excess = round(abs(number_of_yes - number_of_no))
        if excess <= run.minimal_order_size:
            break

        market_bids = await get_market_bids(token_pair)
        bet = YES if number_of_yes > number_of_no else NO
        bid = market_bids[bet]

        if bid < MIN_SELLABLE_BID:
            # No real bid on the book: selling here would be donating the
            # position.  Leave it (and let the risk layer decide).
            log.log(
                "neutralise_no_bid",
                condition_id=market.condition_id,
                token="YES" if bet == YES else "NO",
                bid=bid,
                excess=excess,
            )
            break

        await _place_order(
            run=run,
            client=client,
            token=token_pair[bet],
            price=bid,
            size=excess,
            side="SELL",
            is_FOK=True,
            engine=engine,
        )
        log.log(
            "neutralise_order",
            condition_id=market.condition_id,
            token="YES" if bet == YES else "NO",
            side="SELL",
            price=bid,
            size=excess,
            attempt=attempt,
        )

        attempt += 1
        await asyncio.sleep(NEUTRALISE_SLEEP_S)
        is_neutral, shares = await position_is_neutral(
            run, client, token_pair, engine
        )

    log.log(
        "neutralise_end",
        condition_id=market.condition_id,
        yes=shares[0],
        no=shares[1],
        neutral=is_neutral,
        attempts=attempt,
    )
    return shares
