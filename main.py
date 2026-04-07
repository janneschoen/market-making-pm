from limits import getLimit

import time, json, requests
from datetime import datetime

with open("config.json", "r") as file:
    data = json.load(file)

POS_SIZE = data["positionSize"]
MAX_ENTRY_DELAY = data["maxEntryDelay"]
BETS = "UP", "DOWN"


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

    window = None
    windowNum = 0

    BASE_LIMITS = []
    for a in range(10, 50, 5):
        BASE_LIMITS.append(a/100)

    limits = {}
    fillPrices = {}
    for base in BASE_LIMITS:
        fillPrices[base] = [None for x in BETS]

    totalPnl = {}
    for base in BASE_LIMITS:
        totalPnl[base] = 0

    while True:
        current = getWindow()
        if current != window:
            if windowNum:
                print(f"WINDOW #{windowNum}")
                for base in BASE_LIMITS:
                    filledAt = fillPrices[base]
                    if filledAt.count(None) == 1:
                        for bet in range(len(BETS)):
                            fillPrice = filledAt[bet]
                            if fillPrice:
                                if prices[bet] > fillPrice:
                                    totalPnl[base] += 1.0 - fillPrice
                                else:
                                    totalPnl[base] -= fillPrice
                    elif filledAt.count(None) == 0:
                        totalPnl[base] += 1.0 - (sum(filledAt))

                    print(f"{base:<4} base | {str(filledAt[0]):<5} + {str(filledAt[1]):<5} | Total PnL: ${totalPnl[base]:<10.4f}")

            windowNum += 1

            for base in BASE_LIMITS:
                fillPrices[base] = [None for x in BETS]

            window = current
            tokens = getTokens(window)

        prices = [getPrice(token) for token in tokens]

        for base in BASE_LIMITS:
            limits[base] = [getLimit(base, x, fillPrices[base]) for x in range(len(BETS))]

        for base in BASE_LIMITS:
            for b in range(len(BETS)):
                if not fillPrices[base][b] and prices[b] <= limits[base][b]:
                    fillPrices[base][b] = limits[base][b]

        time.sleep(1)

if __name__ == "__main__":
    main()