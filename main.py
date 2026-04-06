from limits import getLimit
from redeem import redeemBets
from clob import initClient, getAccountValue, getTokenBalance, updateOrder, getFilled

import time, json, requests, websocket
from py_clob_client.client import ClobClient
from datetime import datetime

with open("config.json", "r") as file:
    data = json.load(file)

POS_SIZE = data["positionSize"]
MAX_ENTRY_DELAY = data["maxEntryDelay"]
BETS = "UP", "DOWN"

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

def getSecondsPassed():
    now = datetime.now()
    return (now.minute % 5) * 60 + now.second

def main():

    secondsPassed = getSecondsPassed()
    while secondsPassed > MAX_ENTRY_DELAY:
        print(f"\rWaiting for new 5m-window: {300-secondsPassed}s  ", end='', flush=True)
        time.sleep(1)
        secondsPassed = getSecondsPassed()

    window = getWindow()
    tokens = getTokens(window)

    filled = [getFilled(client, t) for t in tokens]
    limits = [getLimit(x, filled) for x in range(len(BETS))]
    betValue = (getAccountValue(client) * POS_SIZE) / 2

    while True:
        shares = [getTokenBalance(client, tokens[x]) for x in range(len(BETS))]
        current = getWindow()
        if current != window:
            filled = [None, None]
            limits = [getLimit(x, filled) for x in range(len(BETS))]
            redeemBets()
            window = current
            tokens = getTokens(window)
            betValue = (getAccountValue(client) * POS_SIZE) / 2
            print("\n=== NEW 5m WINDOW ===")
            print("-> Bet value: $", betValue)

        filled = [getFilled(client, t) for t in tokens]

        print(time.strftime("\n%H:%M:%S"))
        print("Shares:", shares)
        print("Fill prices:", filled)

        limits = [getLimit(x, filled) for x in range(len(BETS))]
        for b in range(len(BETS)):
            print(BETS[b], "limit", limits[b])
        
        for b in range(len(BETS)):
            if not filled[b]:
                updateOrder(client, tokens[b], limits[b], betValue)

        time.sleep(1)

if __name__ == "__main__":
    main()