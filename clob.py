from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType, TradeParams, OrderType, OrderArgs
from py_clob_client.order_builder.constants import BUY, SELL

from dotenv import load_dotenv
import os, requests

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

def getTokenBalance(client, token):
    params = BalanceAllowanceParams(
        asset_type = AssetType.CONDITIONAL,
        token_id = token
    )

    tokenBalance = client.get_balance_allowance(params=params)
    shares = int(tokenBalance.get("balance", 0)) / 1_000_000

    return shares

def updateOrder(client, token, limit, investment):
    
    cancelResponse = client.cancel_market_orders(
        asset_id = token,
    )
    print("Cancelled old order:", cancelResponse)

    size = investment / limit
    if size < 5:
        print("Error: desired size equals less than 5 shares.")
        return

    signedOrder = client.create_order(
        OrderArgs(
            token_id = token,
            price = limit,
            size = size,
            side = BUY,
        )
    )

    print(f"Opening limit order for {size} ({investment} / {limit}) at $ {limit}")

    response = client.post_order(signedOrder, OrderType.GTC)

    print("Order Response:", response)

def getFilled(client, token):
    params = TradeParams()
    if token:
        params.asset_id = token

    trades = client.get_trades(params)
    if not len(trades):
        return None

    return float(trades[0]["price"])