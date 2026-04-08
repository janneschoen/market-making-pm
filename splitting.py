import os
from dotenv import load_dotenv
from eth_abi import encode as eth_encode
from eth_utils import keccak
from py_builder_relayer_client.client import RelayClient
from py_builder_relayer_client.models import RelayerTxType, OperationType, SafeTransaction
from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds


# Constants (Polygon Mainnet)
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
RELAYER_URL = "https://relayer-v2.polymarket.com"
CHAIN_ID = 137


def initRelayClient():
    wallet_type = RelayerTxType.SAFE

    client = RelayClient(
        RELAYER_URL,
        137,
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
    return client

def createSplit(relayClient, conditionId, size):
    amount = size * 10**6
    
    approve_data = (
        keccak(text="approve(address,uint256)")[:4]
        + encode(["address", "uint256"], [CTF_ADDRESS, 2**256 - 1])  # max uint256
    )

    approve_txn = SafeTransaction(
        to=USDC_ADDRESS,
        operation=OperationType.Call,
        data="0x" + approve_data.hex(),
        value="0",
    )

    args = eth_encode(
        ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
        [
            USDC_ADDRESS,
            b"\x00" * 32,                    # parentCollectionId = zero
            bytes.fromhex(conditionId[2:]), # conditionId as bytes
            [1, 2],                          # Yes / No partition
            amount
        ]
    )

    selector = keccak(text="splitPosition(address,bytes32,bytes32,uint256[],uint256)")[:4]

    txn = SafeTransaction(
        to=CTF_ADDRESS,
        operation=OperationType.Call,
        data="0x" + (selector + args).hex(),
        value="0",
    )

    print(f"Submitting split of ${amount / 1e6} USDC.e into Yes/No pairs...")
    response = relayClient.execute([txn], f"Split {amount / 1e6} USDC")
    result = response.wait()

    print("Split completed successfully!")
    print("Transaction hash / details:", result)
    return result

if __name__ == "__main__":
    split_position()