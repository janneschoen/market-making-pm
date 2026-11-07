"""CLOB client initialisation and authentication."""
from __future__ import annotations

import os

from dotenv import load_dotenv
from py_clob_client.client import ClobClient

CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137


def init_client() -> ClobClient:
    """
    Build and authenticate a CLOB client from environment variables.

    Expects:
      POLYMARKET_KEY    — private key (hex string)
      POLYMARKET_FUNDER — funder wallet address (0x…)

    Credentials are env-only; `.env` is git-ignored (ROADMAP §8).
    """
    load_dotenv()

    client = ClobClient(
        host=CLOB_HOST,
        signature_type=1,
        chain_id=POLYGON_CHAIN_ID,
        key=os.getenv("POLYMARKET_KEY"),
        funder=os.getenv("POLYMARKET_FUNDER"),
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    return client
