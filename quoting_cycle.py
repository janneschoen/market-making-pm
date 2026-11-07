"""
Continuous quoting loop for a single binary-outcome market.

Core idea — inventory-skewed market making:
  1. Quote two-sided: place BUY orders on both YES and NO tokens.
     Buying NO at price p is economically equivalent to selling YES at 1-p.
  2. Skew the own-midpoint linearly away from the market-midpoint based on
     net inventory: own_mid = market_mid − (inventory × skew_intensity).
     This tilts quotes to incentivise mean-reversion of inventory.
  3. Refresh quotes every `refresh_rate` seconds: cancel old orders, post new.

Paper mode (engine != None): orders go to SimulationEngine instead of CLOB.
"""
from market_info import get_hours_to_resolution, get_market_bids, Market
from market_action import cancel_order, place_order, get_token_balance, get_open_orders
from simulation.engine import SimulationEngine
from datetime import datetime
from config import YES, NO, Config
import asyncio


async def _get_balance(run, client, token, engine):
    """Get token balance — from engine (paper) or CLOB (live)."""
    if engine is not None:
        return engine.get_balance(token)
    return await get_token_balance(client, token)


async def _get_open_orders(run, client, market, engine):
    """Get open orders — from engine (paper) or CLOB (live)."""
    if engine is not None:
        return engine.get_open_orders(market)
    return await get_open_orders(client, market)


async def _place_order(run, client, token, price, size, side, is_FOK, engine):
    """Place order — to engine (paper) or CLOB (live)."""
    if engine is not None:
        return engine.place_order(run, client, token, price, size, side, is_FOK)
    return await place_order(run, client, token, price, size, side, is_FOK)


async def _cancel_order(run, client, order_id, engine):
    """Cancel order — in engine (paper) or CLOB (live)."""
    if engine is not None:
        engine.cancel_order(order_id)
        return
    return await cancel_order(client, order_id)


async def do_quoting_cycle(run: Config, client, market: Market,
                           engine: SimulationEngine = None):
    """
    Run the quoting loop until the market exits the trading window.

    Algorithm per tick:
      - Fetch best market bid for YES and NO
      - Imply YES-ask from NO-bid (1 − NO_bid) since the payoff is 1 between them
      - Compute market mid and spread
      - Measure net inventory (YES_held − NO_held)
      - Shift own-mid:  own_mid = market_mid − (inventory × skew_intensity)
      - Post two BUY orders:
          * Buy YES at own_mid − spread/2
          * Buy NO  at 1 − (own_mid + spread/2)    (≡ sell YES at own_mid + spread/2)
      - Cancel previous orders (replace, don't amend, to avoid gaps)
    """
    hours_to_resolution = await get_hours_to_resolution(market)
    while hours_to_resolution > run.trading_window[1]:

        # --- Fetch market state ---
        yes_bid, no_bid = await get_market_bids(market.token_pair)

        # Implied YES ask: the complement of the best NO bid.
        # If someone will buy NO at price p, they effectively sell YES at 1−p.
        yes_ask = 1 - no_bid

        market_spread = yes_ask - yes_bid
        market_mid = (yes_bid + yes_ask) / 2

        # --- Simulation: check fills from previous cycle ---
        if engine is not None:
            engine.check_fills(market, [yes_bid, no_bid], hours_to_resolution)
            engine.total_quote_cycles += 1
            engine.latest_mids[market.condition_id] = market_mid
            engine.latest_spreads[market.condition_id] = market_spread

        # --- Inventory & skew ---
        number_of_yes = await _get_balance(run, client, market.token_pair[YES], engine)
        number_of_no = await _get_balance(run, client, market.token_pair[NO], engine)

        # Net exposure: positive = long YES, negative = short YES (long NO)
        bet_exposure = number_of_yes - number_of_no

        # Skew own-midpoint away from market-midpoint proportional to exposure.
        # If long YES → own_mid < market_mid → bid lower on YES → discourage further buys.
        # If short YES → own_mid > market_mid → bid higher on YES → attract YES sells.
        own_mid = market_mid - (bet_exposure * run.skew_intensity)

        # --- Quote construction ---
        # Two BUY orders that together create a two-sided market:
        #   YES bid = own_mid − spread/2   (buy YES cheap)
        #   NO  bid = 1 − (own_mid + spread/2)
        #            = complement of selling YES at own_mid + spread/2
        own_quotes = [None, None]
        own_quotes[YES] = own_mid - run.spread / 2
        own_quotes[NO] = 1 - (own_mid + run.spread / 2)

        # --- Order management ---
        # Fetch currently open orders BEFORE placing new ones so we can cancel
        # the old set after posting.  This avoids a time-window with zero liquidity.
        previous_orders = await _get_open_orders(run, client, market, engine)

        for bet in YES, NO:
            await _place_order(
                run,
                client,
                token = market.token_pair[bet],
                price = own_quotes[bet],
                size = run.standard_order_size,
                side = "BUY", # YES and NO cancel each other => both buy orders
                is_FOK = False,
                engine = engine,
            )

        # Store quotes for TUI display
        if engine is not None:
            engine.latest_quotes[market.condition_id] = own_quotes
            engine.latest_own_mids[market.condition_id] = own_mid

        # Cancel stale orders after posting fresh ones (no liquidity gap)
        for order in previous_orders:
            await _cancel_order(run, client, order, engine)

        print(f"\nMarket: {market.question} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Inventory: {number_of_yes} Y, {number_of_no} N ({round(bet_exposure, 2)})")
        print("──────────────────────────────────────────────────────────────")
        print(f"{'':<13} {'Yes':>10} {'No':>10} {'Spread':>10} {'Midpoint':>12}")
        print("──────────────────────────────────────────────────────────────")
        print(f"{'Market':<13} {yes_bid:>10.4f} {no_bid:>10.4f} {market_spread:>10.4f} {market_mid:>12.4f}")
        print(f"{'My Quotes':<13} {own_quotes[YES]:>10.4f} {own_quotes[NO]:>10.4f} {run.spread:>10.4f} {own_mid:>12.4f}")
        print("──────────────────────────────────────────────────────────────")

        await asyncio.sleep(run.refresh_rate)
        hours_to_resolution = await get_hours_to_resolution(market)
