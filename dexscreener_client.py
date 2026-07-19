"""
dexscreener_client.py — Dexscreener API wrapper (no API key required).

Covers multi-chain token data:
  - Token search by symbol/name
  - Pair data (price, volume, liquidity, price change)
  - New pairs (useful for zombie revival detection and early wallet signals)
  - Trending pairs by chain

Dexscreener API: https://docs.dexscreener.com/api/reference
Rate limit: ~300 req/min (no auth)
"""

import asyncio
import logging
from typing import Optional
import aiohttp
from config import DEXSCREENER_BASE_URL, MONITORED_CHAINS

logger = logging.getLogger(__name__)


class DexscreenerClient:
    """Async HTTP client for the Dexscreener public API."""

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            headers={"accept": "application/json"}
        )
        return self

    async def __aexit__(self, *_):
        if self._session:
            await self._session.close()

    async def _get(self, url: str, params: dict = None) -> dict | list:
        if not self._session:
            raise RuntimeError("Use DexscreenerClient as an async context manager.")
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status == 429:
                    logger.warning("Dexscreener rate limited — backing off 10s")
                    await asyncio.sleep(10)
                    return {}
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientError as e:
            logger.error("Dexscreener request error: %s", e)
            return {}

    # ─── Token/Pair lookups ────────────────────────────────────────────────────

    async def get_pairs_by_token(self, token_address: str) -> list[dict]:
        """
        All DEX pairs that include this token, across all chains.
        Returns list of pair objects with price, volume, liquidity, etc.
        """
        url = f"{DEXSCREENER_BASE_URL}/tokens/{token_address}"
        data = await self._get(url)
        return data.get("pairs", []) if isinstance(data, dict) else []

    async def get_pairs_by_tokens_batch(
        self, token_addresses: list[str]
    ) -> dict[str, list[dict]]:
        """
        Batched lookup. Calls Dexscreener's /tokens/{addrs} endpoint with up to
        30 comma-separated addresses per request (the documented batch limit).

        Returns {token_address: [pair_objects]}. Tokens with no pairs map to [].
        Pairs are matched to inputs via baseToken.address first (the typical
        case for memecoins paired against SOL/USDC), then quoteToken.address
        as a fallback for the unusual case where the queried token is the quote.

        Rate limit: this endpoint shares the 300 req/min pool with single-token
        calls, so 30x fewer HTTP requests per N tokens.
        """
        if not token_addresses:
            return {}

        # Dedupe while preserving order
        unique = list(dict.fromkeys(token_addresses))
        out: dict[str, list[dict]] = {addr: [] for addr in unique}

        # Chunk into groups of 30 per Dexscreener docs
        for i in range(0, len(unique), 30):
            chunk = unique[i:i + 30]
            chunk_set = set(chunk)
            url = f"{DEXSCREENER_BASE_URL}/tokens/{','.join(chunk)}"
            data = await self._get(url)
            pairs = data.get("pairs", []) if isinstance(data, dict) else []

            for pair in pairs:
                base_addr = (pair.get("baseToken") or {}).get("address")
                if base_addr in chunk_set:
                    out[base_addr].append(pair)
                    continue
                quote_addr = (pair.get("quoteToken") or {}).get("address")
                if quote_addr in chunk_set:
                    out[quote_addr].append(pair)

        return out

    @staticmethod
    def _pairs_to_stats(pairs: list[dict], preferred_chain: str = "solana") -> dict:
        """Pure helper. Picks the best pair (preferred_chain + highest liquidity)
        and flattens it into the stats dict shape returned by get_token_stats."""
        if not pairs:
            return {}
        chain_pairs = [p for p in pairs if p.get("chainId", "").lower() == preferred_chain.lower()]
        pool = chain_pairs if chain_pairs else pairs
        pool.sort(key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
        best = pool[0]
        return {
            "chain":           best.get("chainId"),
            "pair_address":    best.get("pairAddress"),
            "base_token":      best.get("baseToken", {}).get("symbol"),
            "token_address":   best.get("baseToken", {}).get("address"),
            "price_usd":       float(best.get("priceUsd", 0) or 0),
            "volume_24h":      float(best.get("volume", {}).get("h24", 0) or 0),
            "volume_6h":       float(best.get("volume", {}).get("h6", 0) or 0),
            "volume_1h":       float(best.get("volume", {}).get("h1", 0) or 0),
            "price_change_1h": float(best.get("priceChange", {}).get("h1", 0) or 0),
            "price_change_6h": float(best.get("priceChange", {}).get("h6", 0) or 0),
            "price_change_24h":float(best.get("priceChange", {}).get("h24", 0) or 0),
            "liquidity_usd":   float(best.get("liquidity", {}).get("usd", 0) or 0),
            "market_cap":      float(best.get("marketCap", 0) or 0),
            "fdv":             float(best.get("fdv", 0) or 0),
            "pair_created_at": best.get("pairCreatedAt"),  # unix ms
            "dex":             best.get("dexId"),
            "url":             best.get("url"),
        }

    async def get_token_stats_batch(
        self, token_addresses: list[str], preferred_chain: str = "solana"
    ) -> dict[str, dict]:
        """
        Batched version of get_token_stats. Returns {token_address: stats_dict}
        in the same shape as get_token_stats. Tokens with no Dexscreener pairs
        are omitted from the result (callers should treat missing keys as
        "no data" rather than zero).
        """
        pairs_map = await self.get_pairs_by_tokens_batch(token_addresses)
        results: dict[str, dict] = {}
        for addr, pairs in pairs_map.items():
            stats = self._pairs_to_stats(pairs, preferred_chain=preferred_chain)
            if stats:
                results[addr] = stats
        return results

    async def get_pair(self, chain_id: str, pair_address: str) -> dict:
        """Single pair by chain + pair address."""
        url = f"{DEXSCREENER_BASE_URL}/pairs/{chain_id}/{pair_address}"
        data = await self._get(url)
        pairs = data.get("pairs", [])
        return pairs[0] if pairs else {}

    async def search_tokens(self, query: str) -> list[dict]:
        """
        Search tokens by name or symbol.
        Returns up to 30 matching pairs.
        """
        url = f"{DEXSCREENER_BASE_URL}/search"
        data = await self._get(url, params={"q": query})
        return data.get("pairs", []) if isinstance(data, dict) else []

    async def get_boosted_tokens(self, chain: str, limit: int = 50) -> list[dict]:
        """
        Latest boosted tokens on a given chain (Dexscreener's "token-boosts" feed).

        NOTE: Dexscreener has no public "recently created pairs" endpoint. This
        returns tokens whose listings have been boosted — a useful proxy for
        "tokens getting attention right now" but NOT a true new-pairs feed.
        """
        url = "https://api.dexscreener.com/token-boosts/latest/v1"
        data = await self._get(url)
        if isinstance(data, list):
            filtered = [p for p in data if p.get("chainId", "").lower() == chain.lower()]
            return filtered[:limit]
        return []

    # Back-compat alias — older code calls get_new_pairs
    get_new_pairs = get_boosted_tokens

    # ─── Volume and price helpers ──────────────────────────────────────────────

    async def get_token_stats(self, token_address: str,
                               preferred_chain: str = "solana") -> dict:
        """
        Returns a flat dict of the most liquid pair's stats for this token.
        Picks the pair with highest liquidity on the preferred chain first.
        """
        pairs = await self.get_pairs_by_token(token_address)
        return self._pairs_to_stats(pairs, preferred_chain=preferred_chain)

    # ─── Zombie detection helpers ──────────────────────────────────────────────

    @staticmethod
    def revival_check_from_stats(stats: dict,
                                  dormant_avg_volume: float,
                                  volume_multiplier: float = 5.0,
                                  min_price_change_pct: float = 20.0) -> bool:
        """Pure-logic revival check. No HTTP. Use when stats were pre-fetched
        via get_token_stats_batch — saves one round-trip per token."""
        if not stats:
            return False
        vol_1h = stats.get("volume_1h", 0) or 0
        price_chg_1h = stats.get("price_change_1h", 0) or 0
        liquidity = stats.get("liquidity_usd", 0) or 0
        dormant_hourly = (dormant_avg_volume or 0) / 24.0
        volume_spike = (
            vol_1h > (dormant_hourly * volume_multiplier)
            if dormant_hourly > 0 else vol_1h > 1_000
        )
        price_spike = price_chg_1h >= min_price_change_pct
        return volume_spike and price_spike and liquidity > 0

    async def detect_zombie_revival(self, token_address: str,
                                     dormant_avg_volume: float,
                                     volume_multiplier: float = 5.0,
                                     min_price_change_pct: float = 20.0,
                                     chain: str = "solana") -> tuple[bool, dict]:
        """
        Checks if a dormant token is reviving.
        Returns (is_reviving, stats_dict).

        Criteria:
          1. Current 1h volume > dormant_avg_volume * volume_multiplier
          2. 1h price change > min_price_change_pct

        Single-token path. For watchlist scans use get_token_stats_batch +
        revival_check_from_stats to avoid one HTTP per token.
        """
        stats = await self.get_token_stats(token_address, preferred_chain=chain)
        is_reviving = self.revival_check_from_stats(
            stats, dormant_avg_volume, volume_multiplier, min_price_change_pct
        )
        return is_reviving, stats

    async def batch_token_stats(self, token_addresses: list[str],
                                 preferred_chain: str = "solana",
                                 concurrency: int = 5) -> dict[str, dict]:
        """Back-compat alias for get_token_stats_batch. `concurrency` is
        ignored — the new implementation uses Dexscreener's native batch
        endpoint (up to 30 addresses per call), which is strictly faster."""
        return await self.get_token_stats_batch(token_addresses, preferred_chain)

    # ─── Multi-chain trending ──────────────────────────────────────────────────

    async def get_trending_across_chains(self, chains: list[str] = None) -> list[dict]:
        """
        Pull token-profile boosted/trending list and return across all monitored chains.
        Useful for cross-chain zombie + wallet signal scanning.
        """
        chains = chains or MONITORED_CHAINS
        url = "https://api.dexscreener.com/token-profiles/latest/v1"
        data = await self._get(url)
        if not isinstance(data, list):
            return []
        return [
            item for item in data
            if item.get("chainId", "").lower() in [c.lower() for c in chains]
        ]
