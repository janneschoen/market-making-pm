from config import NEUTRAL_NUM

from py_builder_relayer_client.client import RelayClient
from py_builder_relayer_client.models import RelayerTxType,OperationType,SafeTransaction
from py_builder_signing_sdk.config import BuilderConfig,BuilderApiKeyCreds
import os

USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
RELAYER_URL = "https://relayer-v2.polymarket.com"
CHAIN_ID = 137
ZERO_BYTES32 = "0x0000000000000000000000000000000000000000000000000000000000000000"

def initRelayClient():
    wallet_type = RelayerTxType.SAFE

    relayClient = RelayClient(
        RELAYER_URL,
        CHAIN_ID,
        os.getenv("POLYMARKET_KEY"),
        BuilderConfig(
            local_builder_creds = BuilderApiKeyCreds(
                key = os.getenv("BUILDER_API"),
                secret = os.getenv("BUILDER_SECRET"),
                passphrase = os.getenv("BUILDER_PASS"),
            )
        ),
        relay_tx_type=wallet_type,
    )
    return relayClient


async def split(relayClient, market):
    # TO BE IMPLEMENTED
    print(f"Split {NEUTRAL_NUM} shares on market '{market['question']}'")


async def merge(relayClient, amount, market):
    merge_abi = [{
    "name": "mergePositions",
    "type": "function",
    "inputs": [
        {"name": "collateralToken", "type": "address"},
        {"name": "parentCollectionId", "type": "bytes32"},
        {"name": "conditionId", "type": "bytes32"},
        {"name": "partition", "type": "uint256[]"},
        {"name": "amount", "type": "uint256"}
    ],
    "outputs": []
    }]

    onchainAmount = amount * 10**6

    merge_tx = {
        "to": CTF_ADDRESS,
        "data": Web3().eth.contract(
            address = CTF_ADDRESS, abi = merge_abi
        ).encode_abi(
            abi_element_identifier = "mergePositions",
            args = [
                USDCe_ADDRESS,
                bytes(32),
                market["conditionId"],
                [1, 2],
                onchainAmount
            ]
        ),
        "value": "0"
    }

    response = relayClient.execute([merge_tx], f"Merged {amount} shares on market: {market['question']}")
    response.wait()