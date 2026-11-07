"""
Low-level Polymarket CLOB actions: balances and order lifecycle.

All monetary amounts from the API are denominated in units of 10^−6
(USDC has 6 decimals on Polygon), so balances are divided by 1e6 for
human-readable units.

Phase 0 additions (ROADMAP §8 order-state machine, §10 quoting bugs):

  * `place_order` returns the CLOB response instead of discarding it, so the
    caller can verify that an order actually rested before cancelling the
    previous quote set
  * price and size are normalised (4 dp / whole shares) to avoid signatures
    or tick-size rejections on values like 0.48000000000000004
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests
from py_clob_client.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    OpenOrderParams,
    OrderArgs,
    OrderType,
)

from config import Config
from engine.markets import DEFAULT_TIMEOUT_S

TOKEN_DECIMALS = 1_000_000
PRICE_DECIMALS = 4


async def get_token_balance(client, token: str) -> float:
    """Number of outcome tokens held (human-readable, i.e. / 1e6)."""
    params = BalanceAllowanceParams(
        asset_type=AssetType.CONDITIONAL,
        token_id=token,
    )
    balance = client.get_balance_allowance(params=params)
    return int(balance.get("balance", 0)) / TOKEN_DECIMALS


async def get_account_value(client) -> float:
    """Portfolio value = USDC cash + mark-to-market position value."""
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    usdc = client.get_balance_allowance(params=params)
    cash_balance = int(usdc.get("balance", 0)) / TOKEN_DECIMALS

    user = os.getenv("POLYMARKET_FUNDER")
    response = requests.get(
        "https://data-api.polymarket.com/value",
        params={"user": user},
        timeout=DEFAULT_TIMEOUT_S,
    )
    data = response.json()
    position_value = float(data[0]["value"]) if data else 0.0

    return cash_balance + position_value


async def get_open_orders(client, market) -> List[str]:
    """Open order IDs for a market (by condition ID)."""
    response = client.get_orders(OpenOrderParams(market=market.condition_id))
    return [order["id"] for order in response]


async def cancel_order(client, order_id: str) -> Any:
    """Cancel a single order by ID; returns the CLOB response."""
    return client.cancel(order_id)


async def place_order(run: Config, client, token: str, price: float, size: float,
                      side: str, is_FOK: bool) -> Optional[Dict[str, Any]]:
    """
    Sign and submit an order to the CLOB.

    Returns the CLOB response dict, or None if the order was skipped as dust.

    is_FOK = True  → Fill-or-Kill (liquidation)
    is_FOK = False → Good-Till-Cancelled, post-only (quoting)
    """
    size = round(float(size))
    if size <= run.minimal_order_size:
        return None

    price = round(float(price), PRICE_DECIMALS)
    print(f"{side} order for {price} * {size}")

    signed_order = client.create_order(
        OrderArgs(token_id=token, price=price, size=size, side=side)
    )
    if is_FOK:
        response = client.post_order(signed_order, OrderType.FOK)
    else:
        response = client.post_order(signed_order, OrderType.GTC, post_only=True)
    return response if isinstance(response, dict) else {"response": response}


def order_id(response: Optional[Dict[str, Any]]) -> Optional[str]:
    """Extract the CLOB order ID from a `place_order` response."""
    if not isinstance(response, dict):
        return None
    for key in ("orderID", "orderId", "order_id", "id"):
        value = response.get(key)
        if value:
            return str(value)
    return None


def order_rested(response: Optional[Dict[str, Any]]) -> bool:
    """
    True if the CLOB acknowledged the order as live/matched.

    A rejected post (insufficient balance, tick-size violation, post-only
    cross) returns `success: false` or no order ID.  The quoting loop uses
    this to decide whether it is safe to cancel the previous quote set.
    """
    if not isinstance(response, dict):
        return False
    if response.get("success") is False:
        return False
    if response.get("errorMsg") or response.get("error"):
        return False
    if order_id(response):
        return True
    status = str(response.get("status", "")).lower()
    if status in {"live", "matched", "delayed", "matched_partially"}:
        return True
    return bool(response.get("transactionsHashes"))
