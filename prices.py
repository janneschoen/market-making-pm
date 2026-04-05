import requests, time, ast
import websocket, json

def getPrice(id):
    resp = requests.get(f"https://clob.polymarket.com/price?token_id={id}&side=BUY").json()
    return float(resp["price"])