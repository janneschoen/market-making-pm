"""
Inventory-skewed market making for a single binary-outcome market.

Core idea:
  1. Quote two-sided: place BUY orders on both YES and NO tokens.
     Buying NO at price p is economically equivalent to selling YES at 1−p.
  2. Skew the own-midpoint linearly away from the market-midpoint based on
     net inventory: own_mid = market_mid − (inventory × skew_intensity).
  3. Refresh quotes every `refresh_rate` seconds: post new, then cancel old.

Phase 0 fixes (ROADMAP §10):
  * the `trading_window` **upper** bound is enforced during quoting, not only
    at discovery
  * new quotes are verified to have rested before the previous set is
    cancelled; if either leg fails, the new set is withdrawn and the old one
    left in place (no empty book, no half-quote)
  * every quote, order and risk block is written to the JSONL event log
  * orders pass through the risk layer before they reach the book

Mode is decided by `engine`: a `SimulationEngine` routes orders to paper
inventory; `None` sends them to the live CLOB.
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

from config import NO, YES, Config
from engine import clock
from engine import orders as clob
from engine.markets import Market, get_hours_to_resolution, get_market_bids
from engine.risk import RiskManager
from measure.logs import get_logger
from simulation.engine import SimulationEngine


# ── Mode-aware plumbing ──────────────────────────────────────────────────────

async def _get_balance(run, client, token, engine):
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


def _order_reference(response, engine) -> Optional[str]:
    """Order ID for a placed order (paper engine returns the ID directly)."""
    if engine is not None:
        return response if isinstance(response, str) else None
    return clob.order_id(response)


# ── Strategy ─────────────────────────────────────────────────────────────────

async def do_quoting_cycle(run: Config, client, market: Market,
                           engine: SimulationEngine = None):
    """
    Run the quoting loop until the market leaves the trading window.

    Per tick:
      - Fetch best market bid for YES and NO
      - Imply YES ask from NO bid (1 − NO_bid), since the payoffs sum to 1
      - Compute market mid and spread
      - Measure net inventory (YES_held − NO_held)
      - Shift own-mid: own_mid = market_mid − (inventory × skew_intensity)
      - Quote BUY YES at own_mid − spread/2 and
              BUY NO  at 1 − (own_mid + spread/2)
    """
    log = get_logger(getattr(run, "events_log_path", "logs/events.jsonl"))
    risk = RiskManager(getattr(run, "risk_limits", None))
    max_hours, min_hours = run.trading_window
    mode = "paper" if engine is not None else "live"

    hours_to_resolution = await get_hours_to_resolution(market)
    while max_hours > hours_to_resolution > min_hours:

        # --- Fetch market state ---
        yes_bid, no_bid = await get_market_bids(market.token_pair)

        # Implied YES ask: the complement of the best NO bid.
        yes_ask = 1.0 - no_bid
        market_spread = yes_ask - yes_bid
        market_mid = (yes_bid + yes_ask) / 2.0

        # --- Simulation: settle fills from the previous cycle ---
        if engine is not None:
            engine.check_fills(market, [yes_bid, no_bid], hours_to_resolution)
            engine.total_quote_cycles += 1
            engine.latest_mids[market.condition_id] = market_mid
            engine.latest_spreads[market.condition_id] = market_spread
            risk.record_pnl(engine.get_pnl())

        # --- Inventory & skew ---
        number_of_yes = await _get_balance(
            run, client, market.token_pair[YES], engine
        )
        number_of_no = await _get_balance(
            run, client, market.token_pair[NO], engine
        )
        bet_exposure = number_of_yes - number_of_no

        # Long YES → lower own-mid → cheaper YES bid, discourages further buys.
        # Short YES → higher own-mid → attracts YES sellers.
        own_mid = market_mid - (bet_exposure * run.skew_intensity)

        # --- Quote construction ---
        own_quotes: List[Optional[float]] = [None, None]
        own_quotes[YES] = own_mid - run.spread / 2.0
        own_quotes[NO] = 1.0 - (own_mid + run.spread / 2.0)
        size = run.standard_order_size

        if engine is not None:
            engine.latest_quotes[market.condition_id] = own_quotes
            engine.latest_own_mids[market.condition_id] = own_mid

        log.quote(
            mode=mode,
            condition_id=market.condition_id,
            question=market.question,
            yes_bid=yes_bid,
            no_bid=no_bid,
            yes_ask=yes_ask,
            market_spread=market_spread,
            market_mid=market_mid,
            own_mid=own_mid,
            yes_quote=own_quotes[YES],
            no_quote=own_quotes[NO],
            quote_spread=run.spread,
            inventory_yes=number_of_yes,
            inventory_no=number_of_no,
            exposure=bet_exposure,
            hours_to_resolution=hours_to_resolution,
        )

        # --- Risk gate (both legs before touching the book) ---
        decisions = [
            (bet, risk.check_order(
                price=own_quotes[bet], size=size, position=abs(bet_exposure)
            ))
            for bet in (YES, NO)
        ]
        blocked = [(bet, decision) for bet, decision in decisions
                   if not decision.allowed]

        previous_orders = await _get_open_orders(run, client, market, engine)

        new_responses: List[object] = []
        failed = bool(blocked)

        for bet, decision in blocked:
            log.risk_block(
                mode=mode,
                condition_id=market.condition_id,
                token="YES" if bet == YES else "NO",
                price=own_quotes[bet],
                size=size,
                reason=decision.reason,
            )

        for bet in (() if blocked else (YES, NO)):
            response = await _place_order(
                run,
                client,
                token=market.token_pair[bet],
                price=own_quotes[bet],
                size=size,
                side="BUY",  # YES and NO buys together form a two-sided market
                is_FOK=False,
                engine=engine,
            )
            new_responses.append(response)

            rested = True if engine is not None else clob.order_rested(response)
            log.order(
                mode=mode,
                condition_id=market.condition_id,
                token="YES" if bet == YES else "NO",
                side="BUY",
                price=own_quotes[bet],
                size=size,
                fok=False,
                order_id=_order_reference(response, engine),
                rested=rested,
                response=response,
            )
            if not rested:
                failed = True
                break  # never leave a half-quote resting
            if engine is not None and response is not None:
                risk.record_fill(own_quotes[bet], size)

        if failed:
            # Withdraw whichever new leg did rest and keep the previous set
            # live: a rejected post must not leave the book empty.
            for response in new_responses:
                reference = _order_reference(response, engine)
                if reference:
                    await _cancel_order(run, client, reference, engine)
            log.log(
                "quote_abandoned",
                mode=mode,
                condition_id=market.condition_id,
                restored_previous=bool(previous_orders),
            )
        else:
            # Cancel stale orders only after fresh ones are confirmed.
            for order in previous_orders:
                await _cancel_order(run, client, order, engine)

        print(f"\nMarket: {market.question} | "
              f"{clock.now().strftime('%Y-%m-%d %H:%M:%S')} UTC")
        print(f"Inventory: {number_of_yes} Y, {number_of_no} N "
              f"({round(bet_exposure, 2)})")
        print("──────────────────────────────────────────────────────────────")
        print(f"{'':<13} {'Yes':>10} {'No':>10} {'Spread':>10} {'Midpoint':>12}")
        print("──────────────────────────────────────────────────────────────")
        print(f"{'Market':<13} {yes_bid:>10.4f} {no_bid:>10.4f} "
              f"{market_spread:>10.4f} {market_mid:>12.4f}")
        print(f"{'My Quotes':<13} {own_quotes[YES]:>10.4f} "
              f"{own_quotes[NO]:>10.4f} {run.spread:>10.4f} {own_mid:>12.4f}")
        print("──────────────────────────────────────────────────────────────")

        await asyncio.sleep(run.refresh_rate)
        hours_to_resolution = await get_hours_to_resolution(market)
