import os
import time
import requests
from datetime import datetime
from dotenv import load_dotenv
from eth_abi import encode as eth_encode
from eth_utils import keccak
from py_builder_relayer_client.client import RelayClient
from py_builder_relayer_client.models import RelayerTxType, OperationType, SafeTransaction
from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds

load_dotenv()

# Contract addresses (Polygon)
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

# Function selectors
REDEEM_SELECTOR = keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
NEG_RISK_REDEEM_SELECTOR = keccak(text="redeemPositions(bytes32,uint256[])")[:4]
signature_type = 1

def redeemBets():
    """
    Redeem all resolved/redeemable positions.
    signature_type: 1 = Email account (Proxy), 0 or 2 = Browser wallet (Gnosis Safe)
    """
    ts = lambda: datetime.now().strftime("%H:%M:%S")
    RELAYER_RETRY_WAIT = 60

    private_key = os.getenv("POLYMARKET_KEY")
    funder_address = os.getenv("POLYMARKET_FUNDER")
    builder_api_key = os.getenv("BUILDER_API")
    builder_secret = os.getenv("BUILDER_SECRET")
    builder_passphrase = os.getenv("BUILDER_PASS")

    if not all([private_key, funder_address, builder_api_key, builder_secret, builder_passphrase]):
        print(f"{ts()} - Missing credentials in .env")
        return 0

    # Choose relayer transaction type
    wallet_type = RelayerTxType.PROXY if signature_type == 1 else RelayerTxType.SAFE

    client = RelayClient(
        "https://relayer-v2.polymarket.com",
        chain_id=137,
        private_key=private_key,
        builder_config=BuilderConfig(
            local_builder_creds=BuilderApiKeyCreds(
                key=builder_api_key,
                secret=builder_secret,
                passphrase=builder_passphrase,
            )
        ),
        relay_tx_type=wallet_type,
    )

    # Fetch redeemable positions
    try:
        response = requests.get(
            "https://data-api.polymarket.com/positions",
            params={"user": funder_address, "redeemable": "true", "sizeThreshold": 0},
            timeout=15,
        )
        if response.status_code in (429, 1015):
            print(f"{ts()} - Rate limited, waiting {RELAYER_RETRY_WAIT}s...")
            time.sleep(RELAYER_RETRY_WAIT)
            response = requests.get(...)  # retry (simplified)

        positions = response.json()
    except Exception as e:
        print(f"{ts()} - Failed to fetch positions: {e}")
        return 0

    # Filter zero-size positions
    positions = [p for p in positions if float(p.get("size", 0)) > 0]
    if not positions:
        print(f"{ts()} - No redeemable positions found")
        return 0

    print(f"{ts()} - Found {len(positions)} redeemable positions")

    redeemed = 0
    for pos in positions:
        cid = pos.get("conditionId") or pos.get("condition_id", "")
        if not cid.startswith("0x"):
            cid = "0x" + cid.lstrip("0x")
        market = pos.get("title", cid[:12])

        try:
            condition_bytes = bytes.fromhex(cid[2:])
            neg_risk = pos.get("negativeRisk")

            if neg_risk is True:
                # Negative risk market
                size_raw = int(float(pos.get("size", 0)) * 1e6)
                outcome_index = int(pos.get("outcomeIndex", 0))
                amounts = [0] * 2
                amounts[outcome_index] = size_raw
                args = eth_encode(["bytes32", "uint256[]"], [condition_bytes, amounts])
                txn = SafeTransaction(
                    to=NEG_RISK_ADAPTER,
                    operation=OperationType.Call,
                    data="0x" + (NEG_RISK_REDEEM_SELECTOR + args).hex(),
                    value="0",
                )
            else:
                # Standard market (redeem both outcomes — only winners pay out)
                args = eth_encode(
                    ["address", "bytes32", "bytes32", "uint256[]"],
                    [USDC_ADDRESS, b"\x00" * 32, condition_bytes, [1, 2]],
                )
                txn = SafeTransaction(
                    to=CTF_ADDRESS,
                    operation=OperationType.Call,
                    data="0x" + (REDEEM_SELECTOR + args).hex(),
                    value="0",
                )

            resp = client.execute([txn], f"redeem {market}")
            resp.wait()  # Wait for confirmation
            print(f"{ts()} - Redeemed: {market}")
            redeemed += 1

            time.sleep(2)  # Be gentle with rate limits

        except Exception as e:
            if "429" in str(e) or "1015" in str(e):
                print(f"{ts()} - Rate limited, waiting {RELAYER_RETRY_WAIT}s...")
                time.sleep(RELAYER_RETRY_WAIT)
            else:
                print(f"{ts()} - Error redeeming {market}: {e}")

    print(f"{ts()} - Finished. Redeemed {redeemed} positions.")
    return redeemed