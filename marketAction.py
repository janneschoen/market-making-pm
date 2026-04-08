from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
from dotenv import load_dotenv
import os

def initClient():
    load_dotenv()

    client = ClobClient(
        host = "https://clob.polymarket.com",
        signature_type = 1,
        chain_id = 137,
        key = os.getenv("POLYMARKET_KEY"),
        funder = os.getenv("POLYMARKET_FUNDER"),
    )

    apiCreds = client.create_or_derive_api_creds()
    client.set_api_creds(apiCreds)

    return client

async def getTokenBalance(client, token):
    params = BalanceAllowanceParams(
        asset_type = AssetType.CONDITIONAL,
        token_id = token
    )

    tokenBalance = client.get_balance_allowance(params=params)
    shares = int(tokenBalance.get("balance", 0)) / 1_000_000

    return shares

def getAccountValue(client):
    params = BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL
    )
    usdc = client.get_balance_allowance(params=params)
    cashBalance = int(usdc.get("balance", 0)) / 1_000_000

    user = os.getenv("POLYMARKET_FUNDER")
    data = requests.get("https://data-api.polymarket.com/value", params={"user": user}).json()
    positionValue = data[0]["value"]

    return cashBalance + positionValue

async def cancelOrders(client, token):
    response = client.cancel_market_orders(
        asset_id = token
    )


async def placeOrder(client, token, price, size, side):

    signedOrder = client.create_order(
        OrderArgs(
            token_id = token,
            price = price,
            size = size,
            side = side,
        )
    )

    response = client.post_order(signedOrder, OrderType.GTC)
