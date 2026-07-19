"""Research-only scanner for liquid tokens with 3-7 day momentum characteristics."""

import asyncio
import json
import logging
from typing import Awaitable, Callable

from config import (
    SWING_POLL_INTERVAL_SEC, SWING_MIN_LIQUIDITY_USD, SWING_MIN_VOLUME_24H_USD,
    SWING_MIN_MARKET_CAP_USD, SWING_MAX_MARKET_CAP_USD, SWING_MIN_PRICE_CHANGE_24H,
    SWING_MAX_PRICE_CHANGE_24H, SWING_MAX_PRICE_CHANGE_1H, SWING_MIN_SCORE,
    SWING_SIGNAL_COOLDOWN_HOURS,
)
from database import create_signal, get_recent_signal
from dexscreener_client import DexscreenerClient
from signal_outcome_tracker import check_route_available_solana, record_signal_outcome

logger = logging.getLogger(__name__)
SignalCallback = Callable[[dict], Awaitable[None]]


def _bounded(value: float, low: float, high: float) -> float:
    return max(0.0, min(1.0, (value - low) / (high - low)))


def score_candidate(stats: dict) -> tuple[float, list[str]]:
    """Return an inspectable 0-100 score. A discovery-feed inclusion is no signal."""
    liq = float(stats.get("liquidity_usd") or 0)
    vol24 = float(stats.get("volume_24h") or 0)
    vol6 = float(stats.get("volume_6h") or 0)
    vol1 = float(stats.get("volume_1h") or 0)
    pc24 = float(stats.get("price_change_24h") or 0)
    pc6 = float(stats.get("price_change_6h") or 0)
    pc1 = float(stats.get("price_change_1h") or 0)
    mcap = float(stats.get("market_cap") or stats.get("fdv") or 0)
    if (liq < SWING_MIN_LIQUIDITY_USD or vol24 < SWING_MIN_VOLUME_24H_USD
            or not SWING_MIN_MARKET_CAP_USD <= mcap <= SWING_MAX_MARKET_CAP_USD
            or not SWING_MIN_PRICE_CHANGE_24H <= pc24 <= SWING_MAX_PRICE_CHANGE_24H
            or pc1 > SWING_MAX_PRICE_CHANGE_1H):
        return 0.0, ["failed_hard_filters"]

    turnover = vol24 / liq
    acceleration = (vol1 * 6 / vol6) if vol6 > 0 else 0.0
    score = (30 * _bounded(liq, SWING_MIN_LIQUIDITY_USD, 500_000)
             + 25 * _bounded(turnover, 1.0, 12.0)
             + 25 * _bounded(pc24, SWING_MIN_PRICE_CHANGE_24H, 80.0)
             + 20 * _bounded(acceleration, 0.8, 2.5))
    return round(score, 1), [
        f"liquidity=${liq:,.0f}", f"24h_turnover={turnover:.1f}x",
        f"24h_momentum={pc24:.1f}%", f"6h_momentum={pc6:.1f}%",
        f"volume_acceleration={acceleration:.1f}x",
    ]


class SwingTracker:
    """Surfaces at most five research candidates per 15-minute discovery cycle."""

    def __init__(self, on_signal: SignalCallback = None):
        self.on_signal = on_signal
        self._running = False

    async def start(self):
        self._running = True
        logger.info("SwingTracker starting: research-only, interval=%ds", SWING_POLL_INTERVAL_SEC)
        async with DexscreenerClient() as dex:
            while self._running:
                try:
                    await self._poll(dex)
                except Exception as exc:
                    logger.error("SwingTracker poll error: %s", exc, exc_info=True)
                await asyncio.sleep(SWING_POLL_INTERVAL_SEC)

    def stop(self):
        self._running = False

    async def _poll(self, dex: DexscreenerClient):
        discovery = await dex.get_trending_across_chains(chains=["solana"])
        addresses = list(dict.fromkeys(
            item.get("tokenAddress") for item in discovery if item.get("tokenAddress")
        ))[:120]
        if not addresses:
            return
        stats_by_token = await dex.get_token_stats_batch(addresses, preferred_chain="solana")
        ranked = []
        for address, stats in stats_by_token.items():
            score, reasons = score_candidate(stats)
            if score >= SWING_MIN_SCORE:
                ranked.append((score, address, stats, reasons))
        for score, address, stats, reasons in sorted(ranked, reverse=True)[:5]:
            if get_recent_signal(address, "swing", SWING_SIGNAL_COOLDOWN_HOURS):
                continue
            symbol = stats.get("base_token") or address[:8]
            route = await check_route_available_solana(address)
            extra = {"score": score, "reasons": reasons, "market_stats": stats,
                     "dexscreener_url": stats.get("url")}
            signal_id = create_signal("swing", "solana", address, symbol,
                                      extra_data=json.dumps(extra), strategy_tag="swing_quality")
            record_signal_outcome(signal_id, "swing", address, "solana",
                                  float(stats.get("price_usd") or 0), route, symbol)
            logger.info("SWING CANDIDATE: %s score=%.1f %s", symbol, score, "; ".join(reasons))
            if self.on_signal:
                await self.on_signal({"signal_id": signal_id, "type": "swing", "chain": "solana",
                                      "token_address": address, "token_symbol": symbol,
                                      "stats": stats, "extra": extra})
