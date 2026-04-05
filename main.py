from limits import getLimit
from redeem import redeemBets
from clob import initClient, getAccountValue, getTokenBalance, updateOrder, getFilled

import time, json, requests, websocket
from py_clob_client.client import ClobClient

BETS = "UP", "DOWN"
POS_SIZE = 0.10

client = initClient()

def getPrice(id):
    resp = requests.get(f"https://clob.polymarket.com/price?token_id={id}&side=BUY").json()
    return float(resp["price"])

def getTokens(window):
    slug = f"btc-updown-5m-{window}"
    resp = requests.get(f"https://gamma-api.polymarket.com/markets?slug={slug}").json()[0]

    ID_string = resp["clobTokenIds"]
    tokens = [x for x in json.loads(ID_string)]

    return tokens[0], tokens[1]

def getWindow():
    return int(time.time() // 300 * 300)

def main():
    window = getWindow()
    tokens = getTokens(window)

    filled = [getFilled(client, t) for t in tokens]
    print(filled)
    filled = [None, None]
    limits = [getLimit(x, filled) for x in range(len(BETS))]
    investment = getAccountValue(client) * POS_SIZE
    print(investment)

    while True:
        shares = [getTokenBalance(client, tokens[x]) for x in range(len(BETS))]
        print("Shares:", shares)
        current = getWindow()
        if current != window:
            print(f"Profit: {1- sum(filled)}")
            filled = [None, None]
            limits = [getLimit(x, filled) for x in range(len(BETS))]
            redeemBets()
            window = current
            tokens = getTokens(window)
            investment = getAccountValue(client) * POS_SIZE
        
        filled = [getFilled(client, t) for t in tokens]
        prices = [getPrice(t) for t in tokens]

        print(time.strftime("\n%H:%M:%S"))
        print("Prices:", prices)
        print("Fills:", filled)

        limits = [getLimit(x, filled) for x in range(len(BETS))]
        for b in range(len(BETS)):
            print(BETS[b], "limit", limits[b])
        
        for b in range(len(BETS)):
            if not filled[b]:
                updateOrder(client, tokens[b], limits[b], investment)

        time.sleep(1)

if __name__ == "__main__":
    main()