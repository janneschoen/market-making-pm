from datetime import datetime


def getLimit(base, bet, fillPrice):
    if fillPrice[bet]:
        return None
    
    otherBet = (bet + 1) % 2

    if not fillPrice[otherBet]:
        return base

    now = datetime.now()
    secondsPassed = (now.minute % 5) * 60 + now.second

    profitZone = 1 - fillPrice[otherBet]

    limit = round(base + (secondsPassed / 300) * (profitZone - base), 3)

    return limit