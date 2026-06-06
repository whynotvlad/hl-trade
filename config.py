import os
import sys
from dotenv import load_dotenv

load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
NETWORK = os.getenv("NETWORK", "testnet")

_BASE_URLS = {
    "mainnet": "https://api.hyperliquid.xyz",
    "testnet": "https://api.hyperliquid-testnet.xyz",
}


def get_base_url() -> str:
    url = _BASE_URLS.get(NETWORK)
    if not url:
        print(f"Unknown NETWORK '{NETWORK}' in .env — must be 'mainnet' or 'testnet'")
        sys.exit(1)
    return url


def validate():
    if not PRIVATE_KEY:
        print("PRIVATE_KEY is not set. Copy .env.example to .env and fill it in.")
        sys.exit(1)
