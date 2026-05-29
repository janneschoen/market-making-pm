from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType, OrderArgs, OrderType, OpenOrderParams
from dotenv import load_dotenv
import os

def init_client():
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
    params = BalanceAllowanceParams(
        asset_type = AssetType.CONDITIONAL,
        token_id = token
    )

    token_balance = client.get_balance_allowance(params=params)
    shares = int(token_balance.get("balance", 0)) / 1_000_000

    return shares

async def get_account_value(client):
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
    response = client.get_orders(
        OpenOrderParams(
            market = market["condition_id"]
        )
    )
    orders = []
    for order in response:
        orders.append(order['id'])
    return orders

async def cancel_order(client, order_id):
    response = client.cancel(order_id)

async def place_order(client, token, price, size, side, is_FOK):
    if size <= MIN_ORDER_SIZE:
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

    return
    if is_FOK:
        response = client.post_order(signed_order, OrderType.FOK)
    else:
        response = client.post_order(signed_order, OrderType.GTC, post_only=True)
