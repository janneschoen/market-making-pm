"""
Low-level Polymarket CLOB actions: auth, balances, orders.

All monetary amounts from the API are denominated in units of 10^−6
(USDC has 6 decimals on Polygon).  We divide by 1e6 for human-readable units.
"""
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType, OrderArgs, OrderType, OpenOrderParams
from dotenv import load_dotenv
from config import Config
import os
import requests

def init_client():
    """
    Initialise and authenticate a CLOB client using env-var credentials.

    Expects:
      POLYMARKET_KEY    — private key (hex string)
      POLYMARKET_FUNDER — funder wallet address (0x…)
    """
    load_dotenv()

    client = ClobClient(
        host = "https://clob.polymarket.com",
        signature_type = 1,
        chain_id = 137,
        key = os.getenv("POLYMARKET_KEY"),
        funder = os.getenv("POLYMARKET_FUNDER"),
    )

    api_credentials = client.create_or_derive_api_creds()
    client.set_api_creds(api_credentials)

    return client

async def get_token_balance(client, token):
    """
    Return the number of outcome tokens held (human-readable, i.e. / 1e6).

    Polymarket conditional tokens are ERC-1155 with 6 decimals.
    """
    params = BalanceAllowanceParams(
        asset_type = AssetType.CONDITIONAL,
        token_id = token
    )

    token_balance = client.get_balance_allowance(params=params)
    tokens_owned = int(token_balance.get("balance", 0)) / 1_000_000

    return tokens_owned

async def get_account_value(client):
    """
    Total portfolio value = USDC cash balance + mark-to-market position value.
    Uses the Polymarket data API for position valuation.
    """
    params = BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL
    )
    usdc = client.get_balance_allowance(params=params)
    cash_balance = int(usdc.get("balance", 0)) / 1_000_000

    user = os.getenv("POLYMARKET_FUNDER")
    data = requests.get("https://data-api.polymarket.com/value", params={"user": user}).json()
    position_value = data[0]["value"]

    return cash_balance + position_value

async def get_open_orders(client, market):
    """Return a list of open order IDs for a given market (condition ID)."""
    response = client.get_orders(
        OpenOrderParams(
            market = market.condition_id
        )
    )
    orders = []
    for order in response:
        orders.append(order['id'])
    return orders

async def cancel_order(client, order_id):
    """Cancel a single order by ID."""
    response = client.cancel(order_id)

async def place_order(run: Config, client, token: str, price: float, size: int, side: str, is_FOK: bool):
    """
    Sign and submit an order to the CLOB.

    Skips orders below minimal_order_size to avoid dust.

    is_FOK = True  → Fill-or-Kill (used for liquidation)
    is_FOK = False → Good-Till-Cancelled, post-only (used for quoting)
    """
    if size <= run.minimal_order_size:
        return

    print(f"{side} order for {price} * {size}")

    signed_order = client.create_order(
        OrderArgs(
            token_id = token,
            price = price,
            size = size,
            side = side,
        )
    )

    if is_FOK:
        response = client.post_order(signed_order, OrderType.FOK)
    else:
        response = client.post_order(signed_order, OrderType.GTC, post_only=True)
