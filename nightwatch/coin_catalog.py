"""Small bundled coin-name fallback and root-relative icon-cache helpers."""

from __future__ import annotations

import os


COIN_CATALOG: dict[str, dict[str, str]] = {'AAVE': {'name': 'Aave'},
 'ADA': {'name': 'Cardano'},
 'ALGO': {'name': 'Algorand'},
 'APE': {'name': 'ApeCoin'},
 'APT': {'name': 'Aptos'},
 'AR': {'name': 'Arweave'},
 'ARB': {'name': 'Arbitrum'},
 'ATOM': {'name': 'Cosmos Hub'},
 'AVAX': {'name': 'Avalanche'},
 'AXS': {'name': 'Axie Infinity'},
 'BCH': {'name': 'Bitcoin Cash'},
 'BLUR': {'name': 'Blur'},
 'BNB': {'name': 'BNB'},
 'BTC': {'name': 'Bitcoin'},
 'CAKE': {'name': 'PancakeSwap'},
 'CELO': {'name': 'Celo'},
 'CHZ': {'name': 'Chiliz'},
 'COMP': {'name': 'Compound'},
 'CRV': {'name': 'Curve DAO'},
 'DASH': {'name': 'Dash'},
 'DOGE': {'name': 'Dogecoin'},
 'DOT': {'name': 'Polkadot'},
 'DYDX': {'name': 'dYdX'},
 'EGLD': {'name': 'MultiversX'},
 'ENJ': {'name': 'Enjin Coin'},
 'ENS': {'name': 'Ethereum Name Service'},
 'EOS': {'name': 'EOS'},
 'ETC': {'name': 'Ethereum Classic'},
 'ETH': {'name': 'Ethereum'},
 'FET': {'name': 'Fetch.ai'},
 'FIL': {'name': 'Filecoin'},
 'FLOW': {'name': 'Flow'},
 'FTM': {'name': 'Fantom'},
 'GMX': {'name': 'GMX'},
 'GRT': {'name': 'The Graph'},
 'HBAR': {'name': 'Hedera'},
 'ICP': {'name': 'Internet Computer'},
 'IMX': {'name': 'Immutable'},
 'INJ': {'name': 'Injective'},
 'IOST': {'name': 'IOST'},
 'IOTA': {'name': 'IOTA'},
 'JASMY': {'name': 'JasmyCoin'},
 'JUP': {'name': 'Jupiter Project'},
 'KAS': {'name': 'Kaspa'},
 'KAVA': {'name': 'Kava'},
 'LDO': {'name': 'Lido DAO'},
 'LINK': {'name': 'Chainlink'},
 'LTC': {'name': 'Litecoin'},
 'MANA': {'name': 'Decentraland'},
 'MASK': {'name': 'Mask Network'},
 'MINA': {'name': 'Mina Protocol'},
 'MKR': {'name': 'Maker'},
 'NEAR': {'name': 'NEAR Protocol'},
 'NEO': {'name': 'NEO'},
 'ONT': {'name': 'Ontology'},
 'OP': {'name': 'Optimism'},
 'PENDLE': {'name': 'Pendle'},
 'PYTH': {'name': 'Pyth Network'},
 'QNT': {'name': 'Quant'},
 'RENDER': {'name': 'Render'},
 'RNDR': {'name': 'Render'},
 'ROSE': {'name': 'Oasis Network'},
 'RSR': {'name': 'Reserve Rights'},
 'RUNE': {'name': 'THORChain'},
 'SAND': {'name': 'The Sandbox'},
 'SEI': {'name': 'Sei'},
 'SNX': {'name': 'Synthetix Network'},
 'SOL': {'name': 'Solana'},
 'STX': {'name': 'Stacks'},
 'SUI': {'name': 'Sui'},
 'SUSHI': {'name': 'Sushi'},
 'TAO': {'name': 'Bittensor'},
 'THETA': {'name': 'Theta Network'},
 'TIA': {'name': 'Celestia'},
 'TON': {'name': 'Toncoin'},
 'TRX': {'name': 'TRON'},
 'TWT': {'name': 'Trust Wallet'},
 'UNI': {'name': 'Uniswap'},
 'VET': {'name': 'VeChain'},
 'WIF': {'name': 'dogwifhat'},
 'WOO': {'name': 'WOO'},
 'XLM': {'name': 'Stellar'},
 'XMR': {'name': 'Monero'},
 'XRP': {'name': 'XRP'},
 'YFI': {'name': 'yearn.finance'},
 'ZEC': {'name': 'Zcash'},
 'ZIL': {'name': 'Zilliqa'}}


_ICON_MULTIPLIER_PREFIXES = ("1000000", "100000", "10000", "1000")


def coin_base_symbol(symbol: str) -> str:
    """Return the local base asset used for icon filenames/cache keys."""
    value = str(symbol or "").upper().strip().replace("/", "").replace("-", "")
    value = value.removesuffix(".P")
    for quote in ("USDT", "USDC"):
        if value.endswith(quote):
            value = value.removesuffix(quote)
            break
    return "".join(character for character in value if character.isalnum())


def coin_remote_symbol(symbol: str) -> str:
    """Map Binance multiplier contracts such as 1000PEPE back to the asset ticker."""
    base = coin_base_symbol(symbol)
    for prefix in _ICON_MULTIPLIER_PREFIXES:
        if base.startswith(prefix) and len(base) > len(prefix):
            candidate = base[len(prefix):]
            if candidate and any(character.isalpha() for character in candidate):
                return candidate
    return base


def coin_name(symbol: str, default: str = "—") -> str:
    return str(COIN_CATALOG.get(coin_base_symbol(symbol), {}).get("name") or default)


def coin_icon_directory() -> str:
    """Resolve project resources independently of the launch directory."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "icons")


def coin_icon_path(symbol: str) -> str:
    base = coin_base_symbol(symbol)
    return os.path.join(coin_icon_directory(), f"{base}.png") if base else ""


def coin_icon_exists(symbol: str) -> bool:
    path = coin_icon_path(symbol)
    return bool(path and os.path.isfile(path))


def coin_icon_bytes(symbol: str) -> bytes:
    path = coin_icon_path(symbol)
    if not path:
        return b""
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return b""
