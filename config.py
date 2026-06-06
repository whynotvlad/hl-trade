import os
import sys
from dotenv import load_dotenv

load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
ACCOUNT_ADDRESS = os.getenv("ACCOUNT_ADDRESS")

_BASE_URLS = {
    "mainnet": "https://api.hyperliquid.xyz",
    "testnet": "https://api.hyperliquid-testnet.xyz",
}


def get_network() -> str:
    return os.getenv("NETWORK", "testnet")


def get_base_url() -> str:
    network = get_network()
    url = _BASE_URLS.get(network)
    if not url:
        print(f"Unknown NETWORK '{network}' — must be 'mainnet' or 'testnet'")
        sys.exit(1)
    return url


def validate():
    if not PRIVATE_KEY:
        print("PRIVATE_KEY is not set. Copy .env.example to .env and fill it in.")
        sys.exit(1)
    if ACCOUNT_ADDRESS and not ACCOUNT_ADDRESS.startswith("0x"):
        print("ACCOUNT_ADDRESS must be a 0x Ethereum address.")
        sys.exit(1)
