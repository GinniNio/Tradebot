from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import aiohttp

from tradebot.config import ProviderBudget


class ProviderError(RuntimeError):
    pass


class DexscreenerClient:
    BASE_URL = "https://api.dexscreener.com"

    def __init__(self, budget: ProviderBudget, session: Any | None = None):
        self.budget = budget
        self.session = session
        self._semaphore = asyncio.Semaphore(budget.max_concurrency)
        self._request_times: list[float] = []

    async def _throttle(self) -> None:
        now = time.monotonic()
        self._request_times = [
            stamp for stamp in self._request_times if now - stamp < 60
        ]
        if len(self._request_times) >= self.budget.rate_budget_per_minute:
            await asyncio.sleep(max(0, 60 - (now - self._request_times[0])))
        self._request_times.append(time.monotonic())

    async def get_json(self, path: str) -> dict[str, Any] | list[Any]:
        for attempt in range(self.budget.max_retries + 1):
            try:
                async with self._semaphore:
                    await self._throttle()
                    timeout = aiohttp.ClientTimeout(total=self.budget.timeout_seconds)
                    session = self.session or aiohttp.ClientSession(timeout=timeout)
                    try:
                        async with session.get(
                            f"{self.BASE_URL}{path}", timeout=timeout
                        ) as response:
                            if response.status == 429 or response.status >= 500:
                                raise ProviderError(f"http_{response.status}")
                            if response.status >= 400:
                                raise ProviderError("http_client_error")
                            return await response.json()
                    finally:
                        if self.session is None:
                            await session.close()
            except (aiohttp.ClientError, asyncio.TimeoutError, ProviderError) as exc:
                if attempt >= self.budget.max_retries:
                    raise ProviderError(type(exc).__name__) from exc
                await asyncio.sleep(min(4.0, 0.25 * (2**attempt)))
        raise ProviderError("retry_exhausted")

    async def discover(self) -> list[dict[str, Any]]:
        payload = await self.get_json("/token-profiles/latest/v1")
        return (
            [
                item
                for item in payload
                if isinstance(item, dict) and item.get("chainId") == "solana"
            ]
            if isinstance(payload, list)
            else []
        )

    async def pairs_for_token(self, token_address: str) -> list[dict[str, Any]]:
        payload = await self.get_json(f"/token-pairs/v1/solana/{token_address}")
        return (
            [item for item in payload if isinstance(item, dict)]
            if isinstance(payload, list)
            else []
        )

    async def pair(self, pair_address: str) -> dict[str, Any]:
        payload = await self.get_json(f"/latest/dex/pairs/solana/{pair_address}")
        pairs = payload.get("pairs", []) if isinstance(payload, dict) else []
        if not pairs:
            raise ProviderError("pair_not_found")
        return pairs[0]


def select_pair(pairs: list[dict[str, Any]]) -> dict[str, Any] | None:
    usable = [
        p
        for p in pairs
        if p.get("pairAddress")
        and p.get("priceUsd") is not None
        and (p.get("liquidity") or {}).get("usd") is not None
    ]

    def liquidity(pair: dict[str, Any]) -> Decimal:
        try:
            return Decimal(str(pair["liquidity"]["usd"]))
        except (InvalidOperation, TypeError, KeyError):
            return Decimal(-1)

    return max(usable, key=liquidity, default=None)


def normalize_pair(pair: dict[str, Any]) -> dict[str, Any]:
    def value(*path: str) -> Any:
        current: Any = pair
        for key in path:
            current = current.get(key) if isinstance(current, dict) else None
        return current

    return {
        "token_symbol": value("baseToken", "symbol"),
        "token_address": value("baseToken", "address"),
        "pair_address": pair.get("pairAddress"),
        "dex_url": pair.get("url"),
        "liquidity_usd": value("liquidity", "usd"),
        "volume_1h_usd": value("volume", "h1"),
        "volume_6h_usd": value("volume", "h6"),
        "volume_24h_usd": value("volume", "h24"),
        "price_usd": pair.get("priceUsd"),
        "price_change_1h_pct": value("priceChange", "h1"),
        "price_change_24h_pct": value("priceChange", "h24"),
        "price_change_6h_pct": value("priceChange", "h6"),
        "market_cap_usd": pair.get("marketCap"),
        "fdv_usd": pair.get("fdv"),
        "captured_at": datetime.now(timezone.utc),
        "raw_payload_json": pair,
    }
