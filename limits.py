from datetime import datetime

BASE_LIMIT = 0.4

def getLimit(bet, fillPrice):
    if fillPrice[bet]:
        return False
    
    otherBet = (bet + 1) % 2

    if not fillPrice[otherBet]:
        return BASE_LIMIT

    now = datetime.now()
    secondsPassed = (now.minute % 5) * 60 + now.second

    profitZone = 1 - fillPrice[otherBet]

    limit = BASE_LIMIT + (secondsPassed / 300) * (profitZone - BASE_LIMIT)

    return limit