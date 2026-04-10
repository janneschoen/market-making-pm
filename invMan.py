from web3 import Web3
from config import NEUTRAL_NUM

def initRelayClient():
    wallet_type = RelayerTxType.SAFE

    relayClient = RelayClient(
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
    return relayClient

CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDCe_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"


async def split(relayClient, market):

    ctf_abi = [{
        "name": "splitPosition",
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

    usdcAmount = NEUTRAL_NUM * 10**6  # USDCe has 6 decimals

    split_tx = {
        "to": CTF_ADDRESS,
        "data": Web3().eth.contract(
            address=CTF_ADDRESS, abi=ctf_abi
        ).encode_abi(
            abi_element_identifier="splitPosition",
            args=[
                USDCe_ADDRESS,
                bytes(32),
                market["conditionId"],
                [1, 2],
                usdcAmount,
            ]
        ),
        "value": "0"
    }

    response = relayClient.execute([split_tx], f"Split {NEUTRAL_NUM} on market: {market["question"]}")
    response.wait()

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

    usdcAmount = amount * 10**6

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
                usdcAmount
            ]
        ),
        "value": "0"
    }

    response = relayClient.execute([merge_tx], f"Merged {amount} shares on market: {market["question"]}")
    response.wait()