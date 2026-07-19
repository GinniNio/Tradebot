"""
birdeye_client.py — Birdeye Data API wrapper.

Covers:
  - Token OHLCV / price history
  - Token overview (volume, liquidity, market cap)
  - Wallet transaction history (which tokens a wallet bought/sold)
  - Multi-chain support (Solana primary; Ethereum, BSC, Base via chain param)

Birdeye docs: https://birdeye.so/data-api
Requires BIRDEYE_API_KEY in .env
"""

import asyncio
import logging
from typing import Optional
import aiohttp
from config import BIRDEYE_API_KEY, BIRDEYE_BASE_URL

logger = logging.getLogger(__name__)

CHAIN_MAP = {
    "solana":   "solana",
    "ethereum": "ethereum",
    "bsc":      "bsc",
    "base":     "base",
}


class BirdeyeClient:
    """Async HTTP client for the Birdeye public API."""

    def __init__(self, api_key: str = BIRDEYE_API_KEY):
        # No warning when key is unset — wallet_tracker uses Helius as primary;
        # Birdeye is only the fallback for users who explicitly add a Birdeye key.
        self.api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None

    # ─── Session lifecycle ─────────────────────────────────────────────────────

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            base_url=BIRDEYE_BASE_URL,
            headers={
                "X-API-KEY": self.api_key,
                "accept":    "application/json",
            }
        )
        return self

    async def __aexit__(self, *_):
        if self._session:
            await self._session.close()

    async def _get(self, path: str, params: dict = None, chain: str = "solana") -> dict:
        if not self._session:
            raise RuntimeError("Use BirdeyeClient as an async context manager.")
        headers = {"x-chain": CHAIN_MAP.get(chain, "solana")}
        try:
            async with self._session.get(path, params=params, headers=headers) as resp:
                if resp.status == 401:
                    if self.api_key:
                        logger.error("Birdeye: invalid API key")
                    # else: user has no Birdeye key, silent fail (Helius covers the path)
                    return {}
                if resp.status == 429:
                    logger.warning("Birdeye: rate limited — backing off 5s")
                    await asyncio.sleep(5)
                    return {}
                resp.raise_for_status()
                data = await resp.json()
                return data.get("data", data)
        except aiohttp.ClientError as e:
            logger.error("Birdeye request error [%s]: %s", path, e)
            return {}

    # ─── Token data ────────────────────────────────────────────────────────────

    async def get_token_overview(self, token_address: str, chain: str = "solana") -> dict:
        """
        Returns price, volume24h, liquidity, mc, etc.
        Endpoint: /defi/token_overview
        """
        return await self._get(
            "/defi/token_overview",
            params={"address": token_address},
            chain=chain
        )

    async def get_token_price(self, token_address: str, chain: str = "solana") -> float:
        """Current USD price for a token."""
        data = await self.get_token_overview(token_address, chain)
        return float(data.get("price", 0.0))

    async def get_ohlcv(self, token_address: str, resolution: str = "15m",
                        limit: int = 96, chain: str = "solana") -> list[dict]:
        """
        OHLCV candles.
        resolution: 1m, 5m, 15m, 1H, 4H, 1D
        Returns list of {unixTime, o, h, l, c, v}
        """
        data = await self._get(
            "/defi/ohlcv",
            params={
                "address":    token_address,
                "type":       resolution,
                "limit":      limit,
            },
            chain=chain
        )
        return data.get("items", []) if isinstance(data, dict) else []

    async def get_volume_history(self, token_address: str,
                                  days: int = 14, chain: str = "solana") -> list[dict]:
        """Daily volume over the last N days (uses 1D OHLCV candles)."""
        candles = await self.get_ohlcv(token_address, resolution="1D", limit=days, chain=chain)
        return [{"date": c.get("unixTime"), "volume_usd": c.get("v", 0)} for c in candles]

    # ─── Wallet data ───────────────────────────────────────────────────────────

    async def get_wallet_transactions(self, wallet_address: str,
                                       limit: int = 50,
                                       chain: str = "solana") -> list[dict]:
        """
        Recent transactions for a wallet address.
        Endpoint: /v1/wallet/tx_list
        Returns raw tx list — filter for swaps client-side.
        """
        data = await self._get(
            "/v1/wallet/tx_list",
            params={
                "wallet": wallet_address,
                "limit":  limit,
            },
            chain=chain
        )
        return data if isinstance(data, list) else data.get("solana", [])

    async def get_wallet_portfolio(self, wallet_address: str,
                                    chain: str = "solana") -> list[dict]:
        """
        Current token holdings for a wallet.
        Endpoint: /v1/wallet/token_list
        """
        data = await self._get(
            "/v1/wallet/token_list",
            params={"wallet": wallet_address},
            chain=chain
        )
        return data.get("items", []) if isinstance(data, dict) else []

    async def get_wallet_gains(self, wallet_address: str,
                                chain: str = "solana") -> dict:
        """
        Win/loss stats for a wallet — good for scoring smart wallet quality.
        Endpoint: /v1/wallet/gain_loss (if available on your tier)
        """
        return await self._get(
            "/v1/wallet/gain_loss",
            params={"wallet": wallet_address},
            chain=chain
        )

    # ─── Token list / trending ─────────────────────────────────────────────────

    async def get_trending_tokens(self, chain: str = "solana", limit: int = 20) -> list[dict]:
        """Trending tokens by 24h volume change."""
        data = await self._get(
            "/defi/trending_tokens",
            params={"limit": limit},
            chain=chain
        )
        return data.get("items", []) if isinstance(data, dict) else []

    async def get_new_listings(self, chain: str = "solana", limit: int = 50) -> list[dict]:
        """Recently listed tokens — useful for seeding zombie watchlist."""
        data = await self._get(
            "/defi/v3/token/new_listing",
            params={"limit": limit},
            chain=chain
        )
        return data.get("items", []) if isinstance(data, dict) else []

    # ─── Helpers ───────────────────────────────────────────────────────────────

    async def is_token_dormant(self, token_address: str, dormant_days: int = 14,
                                chain: str = "solana") -> tuple[bool, float]:
        """
        Returns (is_dormant, avg_daily_volume_usd).

        Dormant means: the token HAS existed for at least `dormant_days` AND its
        average daily volume over that window is below $5k.

        Brand-new tokens with no history are NOT dormant — they're just new.
        Returns (False, 0.0) when there's insufficient history.
        """
        history = await self.get_volume_history(token_address, days=dormant_days, chain=chain)
        # Require at least half the window of candles before calling it dormant
        min_candles = max(2, dormant_days // 2)
        if not history or len(history) < min_candles:
            return False, 0.0
        volumes = [d["volume_usd"] for d in history if d.get("volume_usd") is not None]
        if not volumes:
            return False, 0.0
        avg_vol = sum(volumes) / len(volumes)
        is_dormant = avg_vol < 5_000
        return is_dormant, avg_vol

    async def get_current_volume_24h(self, token_address: str,
                                      chain: str = "solana") -> float:
        overview = await self.get_token_overview(token_address, chain)
        return float(overview.get("volume24h", 0.0) or overview.get("v24hUSD", 0.0))
