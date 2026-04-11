from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType, OrderArgs, OrderType, OpenOrderParams
from dotenv import load_dotenv
import os
from config import EXPOSURE_TOL, MIN_ORDER_SIZE, NEUTRAL_NUM

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

async def isNeutral(client, tokenPair):
    yesTokens = await getTokenBalance(client, tokenPair[0])
    noTokens = await getTokenBalance(client, tokenPair[1])
    if abs(yesTokens - noTokens) < EXPOSURE_TOL:
        if abs(NEUTRAL_NUM - noTokens) < EXPOSURE_TOL:
            return True
    return False

async def getAccountValue(client):
    params = BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL
    )
    usdc = client.get_balance_allowance(params=params)
    cashBalance = int(usdc.get("balance", 0)) / 1_000_000

    user = os.getenv("POLYMARKET_FUNDER")
    data = requests.get("https://data-api.polymarket.com/value", params={"user": user}).json()
    positionValue = data[0]["value"]

    return cashBalance + positionValue

async def getOpenOrders(client, market):
    response = client.get_orders(
        OpenOrderParams(
            market = market["conditionId"]
        )
    )
    orders = []
    for order in response:
        orders.append(order['id'])
    return orders

async def cancelOrder(client, orderId):
    response = client.cancel(orderId)

async def placeOrder(client, token, price, size, side, isExit):
    if size <= MIN_ORDER_SIZE:
        return

    signedOrder = client.create_order(
        OrderArgs(
            token_id = token,
            price = price,
            size = size,
            side = side,
        )
    )

    if isExit:
        response = client.post_order(signedOrder, OrderType.FOK)
    else:
        response = client.post_order(signedOrder, OrderType.GTC, post_only=True)
