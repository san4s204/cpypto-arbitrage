"""
Configuration module for the Crypto Arbitrage Bot.
Handles environment variables, constants, and configuration settings.
"""
import os
from dotenv import load_dotenv
from cryptography.fernet import Fernet
import logging

# Load environment variables
load_dotenv()

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("crypto_arb_bot.log")
    ]
)
logger = logging.getLogger("crypto_arb_bot")

# API Key Encryption
def get_encryption_key():
    """Get or generate encryption key for API keys."""
    key_file = ".key.enc"
    if os.path.exists(key_file):
        with open(key_file, "rb") as f:
            return f.read()
    else:
        key = Fernet.generate_key()
        with open(key_file, "wb") as f:
            f.write(key)
        return key

ENCRYPTION_KEY = get_encryption_key()
cipher_suite = Fernet(ENCRYPTION_KEY)

def encrypt_api_key(api_key):
    """Encrypt API key using AES-256."""
    return cipher_suite.encrypt(api_key.encode()).decode()

def decrypt_api_key(encrypted_api_key):
    """Decrypt API key."""
    return cipher_suite.decrypt(encrypted_api_key.encode()).decode()

# Exchange Configuration
EXCHANGES = {
    "okx": {
        "api_key": os.getenv("OKX_API_KEY", ""),
        "api_secret": os.getenv("OKX_API_SECRET", ""),
        "password": os.getenv("OKX_PASSWORD", ""),
        "taker_fee": float(os.getenv("OKX_TAKER_FEE", "0.001")),
        "maker_fee": float(os.getenv("OKX_MAKER_FEE", "0.0008")),
    },
    "bybit": {
        "api_key": os.getenv("BYBIT_API_KEY", ""),
        "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "taker_fee": float(os.getenv("BYBIT_TAKER_FEE", "0.001")),
        "maker_fee": float(os.getenv("BYBIT_MAKER_FEE", "0.0008")),
    },
    "htx": {
        "api_key": os.getenv("HTX_API_KEY", ""),
        "api_secret": os.getenv("HTX_API_SECRET", ""),
        "taker_fee": float(os.getenv("HTX_TAKER_FEE", "0.001")),
        "maker_fee": float(os.getenv("HTX_MAKER_FEE", "0.0008")),
    },
    "uniswap": {
        "wallet_address": os.getenv("UNISWAP_WALLET_ADDRESS", ""),
        "private_key": os.getenv("UNISWAP_PRIVATE_KEY", ""),
        "rpc_url": os.getenv("UNISWAP_RPC_URL", ""),
        "fee": float(os.getenv("UNISWAP_FEE", "0.003")),
    },
    "1inch": {
        "wallet_address": os.getenv("ONEINCH_WALLET_ADDRESS", ""),
        "private_key": os.getenv("ONEINCH_PRIVATE_KEY", ""),
        "rpc_url": os.getenv("ONEINCH_RPC_URL", ""),
        "fee": float(os.getenv("ONEINCH_FEE", "0.003")),
    }
}

# Trading Parameters
MIN_PROFIT_MARGIN = float(os.getenv("MIN_PROFIT_MARGIN", "0.003"))  # 0.3%
MAX_CAPITAL_PER_TRADE = float(os.getenv("MAX_CAPITAL_PER_TRADE", "0.1"))  # 10%
MIN_24H_VOLUME = float(os.getenv("MIN_24H_VOLUME", "400000"))  # $400,000
MAX_BID_ASK_SPREAD = float(os.getenv("MAX_BID_ASK_SPREAD", "0.004"))  # 0.4%
VOLATILITY_THRESHOLD = float(os.getenv("VOLATILITY_THRESHOLD", "0.03"))  # 3%
VOLATILITY_WINDOW = int(os.getenv("VOLATILITY_WINDOW", "300"))  # 5 minutes in seconds
MAX_TRANSFER_TIME = int(os.getenv("MAX_TRANSFER_TIME", "60"))  # 60 seconds

# Redis Configuration
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")

# PostgreSQL Configuration
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_DB = os.getenv("POSTGRES_DB", "crypto_arb_bot")
POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")

# Telegram Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")

# Top 50 Trading Pairs (by volume)
TOP_TRADING_PAIRS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT",
    "ADA/USDT", "AVAX/USDT", "DOGE/USDT", "DOT/USDT", "MATIC/USDT",
    "LINK/USDT", "LTC/USDT", "UNI/USDT", "ATOM/USDT", "ETC/USDT",
    "XLM/USDT", "NEAR/USDT", "ALGO/USDT", "FIL/USDT", "APE/USDT",
    "MANA/USDT", "SAND/USDT", "AXS/USDT", "AAVE/USDT", "EGLD/USDT",
    "XMR/USDT", "THETA/USDT", "EOS/USDT", "CAKE/USDT", "XTZ/USDT",
    "ZEC/USDT", "FLOW/USDT", "KCS/USDT", "NEO/USDT", "IOTA/USDT",
    "BTT/USDT", "KLAY/USDT", "BSV/USDT", "DASH/USDT", "MKR/USDT",
    "XEM/USDT", "HNT/USDT", "CHZ/USDT", "BAT/USDT", "ENJ/USDT",
    "ZIL/USDT", "WAVES/USDT", "COMP/USDT", "QTUM/USDT", "OMG/USDT"
]

# Service Ports
MARKET_DATA_PORT = int(os.getenv("MARKET_DATA_PORT", "8001"))
ARB_ENGINE_PORT = int(os.getenv("ARB_ENGINE_PORT", "8002"))
EXECUTION_PORT = int(os.getenv("EXECUTION_PORT", "8003"))
FUNDS_ROUTER_PORT = int(os.getenv("FUNDS_ROUTER_PORT", "8004"))
TG_MANAGER_PORT = int(os.getenv("TG_MANAGER_PORT", "8005"))
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8000"))
