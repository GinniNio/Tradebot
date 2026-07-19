"""
graduation_tracker.py — Launch Momentum Tracker

Detects tokens that have just graduated from the pump.fun bonding curve
to an on-chain AMM (Raydium or PumpSwap) and fires early-entry signals
for measurement via signal_outcomes.

Detection path:
  1. Poll pump.fun's public API for recently graduated coins (complete=True).
  2. Filter to tokens graduated within LAUNCH_MAX_AGE_MINUTES.
  3. Batch-fetch Dexscreener stats for unseen mints.
  4. Apply entry filters; fire signal if all pass.

Detection lag: ~1-2 minutes (Dexscreener indexing + poll interval).
Helius websocket (available post-2026-06-20) will reduce this to ~10s.

Entry filters applied at signal time:
  - pair_created_at age < LAUNCH_MAX_AGE_MINUTES (fresh on Dexscreener)
  - liquidity >= LAUNCH_MIN_LIQUIDITY_USD
  - market_cap <= LAUNCH_MAX_MARKET_CAP_USD  (hasn't already run 7x+)
  - volume_1h >= LAUNCH_MIN_VOLUME_USD        (real buying, not a ghost listing)

Every signal creates a signal_outcomes row measuring returns at
+5m / +15m / +1h / +24h via the existing pending_price_checks loop.
Strategy state is RESEARCH — signals log but cannot open positions.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Callable, Awaitable

import aiohttp

from dexscreener_client import DexscreenerClient
from database import create_signal, get_recent_launch_signal
from signal_outcome_tracker import record_signal_outcome, check_route_available_solana
from telegram_notifier import notify_error
from config import (
    LAUNCH_POLL_INTERVAL_SEC,
    LAUNCH_MAX_AGE_MINUTES,
    LAUNCH_MIN_LIQUIDITY_USD,
    LAUNCH_MAX_MARKET_CAP_USD,
    LAUNCH_MIN_VOLUME_USD,
)

logger = logging.getLogger(__name__)

PUMPFUN_API_URL = "https://frontend-api-v3.pump.fun/coins"
PUMPFUN_TIMEOUT = aiohttp.ClientTimeout(total=10)

SignalCallback = Callable[[dict], Awaitable[None]]


class GraduationTracker:
    """
    Watches for pump.fun token graduations and fires launch signals.

    Graduates are tokens whose bonding curve filled (~$69k market cap),
    causing pump.fun to deploy liquidity into a Raydium or PumpSwap pool.
    The graduation event is the starting gun for launch momentum trading.
    """

    def __init__(self, on_signal: SignalCallback = None):
        self.on_signal = on_signal
        self._running = False
        # In-memory dedup set — prevents re-evaluating the same mint each poll
        # cycle. Pruned at 2000 entries. DB-level dedup (get_recent_launch_signal)
        # handles the restart case for tokens still within the age window.
        self._seen_mints: set[str] = set()
        # Consecutive cycles where _fetch_recent_graduates returned nothing.
        # pump.fun graduates tokens many times per hour, so a sustained empty
        # streak means the fetch/parse path is broken (HTTP errors, Cloudflare
        # blocks, API shape change), NOT a quiet market. The 2026-06-05 to
        # 2026-06-11 silent failure (zero launch signals, errors logged only
        # at debug level) is exactly what this catches.
        self._empty_streak = 0
        # Alert thresholds in cycles (60s each): first warn at 1h, re-warn daily.
        self._empty_streak_first_alert = 60
        self._empty_streak_realert_every = 1440
        # Most recent fetch failure detail (HTTP status or exception repr),
        # surfaced in the streak alert so diagnosis doesn't require DEBUG logs.
        self._last_fetch_problem = "none recorded"

    # ─── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        logger.info(
            "GraduationTracker starting -- pump.fun polling, %dm age window, %ds interval",
            LAUNCH_MAX_AGE_MINUTES, LAUNCH_POLL_INTERVAL_SEC,
        )
        self._running = True
        await self._run_loop()

    def stop(self):
        self._running = False

    # ─── Main loop ─────────────────────────────────────────────────────────────

    async def _run_loop(self):
        async with DexscreenerClient() as dex:
            while self._running:
                try:
                    await self._poll_graduates(dex)
                except Exception as e:
                    logger.error("GraduationTracker error: %s", e, exc_info=True)
                await asyncio.sleep(LAUNCH_POLL_INTERVAL_SEC)

    async def _poll_graduates(self, dex: DexscreenerClient):
        """Fetch recent pump.fun graduates and evaluate each unseen one."""
        graduates = await self._fetch_recent_graduates()
        if not graduates:
            self._empty_streak += 1
            s = self._empty_streak
            if s == self._empty_streak_first_alert or (
                s > self._empty_streak_first_alert
                and (s - self._empty_streak_first_alert)
                % self._empty_streak_realert_every == 0
            ):
                hours = s * LAUNCH_POLL_INTERVAL_SEC / 3600
                logger.warning(
                    "GraduationTracker: %d consecutive empty fetches (%.1fh). "
                    "pump.fun API likely broken or blocked. Last problem: %s",
                    s, hours, self._last_fetch_problem,
                )
                notify_error(
                    "graduation_tracker_empty",
                    f"Graduation tracker: no pump.fun graduates fetched for "
                    f"{hours:.1f}h ({s} cycles). Last problem: "
                    f"{self._last_fetch_problem}. launch_momentum is "
                    f"accumulating zero outcomes.",
                )
            return
        self._empty_streak = 0

        fresh = [g for g in graduates if g["mint"] not in self._seen_mints]
        if not fresh:
            return

        addrs = [g["mint"] for g in fresh]
        try:
            stats_map = await dex.get_token_stats_batch(addrs, preferred_chain="solana")
        except Exception as e:
            logger.warning("GraduationTracker: Dexscreener batch failed: %s", e)
            return

        signaled = 0
        for grad in fresh:
            mint = grad["mint"]
            self._seen_mints.add(mint)
            stats = stats_map.get(mint)
            if not stats:
                continue
            try:
                fired = await self._evaluate_graduate(grad, stats)
                if fired:
                    signaled += 1
            except Exception as e:
                logger.warning("evaluate_graduate error for %s: %s", mint[:8], e)

        logger.info(
            "GraduationTracker poll: %d graduates fetched, %d unseen, %d signaled",
            len(graduates), len(fresh), signaled,
        )

        # Prune seen set to prevent unbounded growth (keeps most recent 1000)
        if len(self._seen_mints) > 2000:
            overflow = list(self._seen_mints)
            self._seen_mints = set(overflow[-1000:])

    # ─── pump.fun API ──────────────────────────────────────────────────────────

    async def _fetch_recent_graduates(self) -> list[dict]:
        """
        Poll pump.fun's public API for recently completed (graduated) tokens.

        Uses sort=last_trade_timestamp with complete=true against the v3 API.
        (graduation_timestamp was removed from v3; king_of_the_hill_timestamp
        is used as the graduation-time proxy for age filtering.)

        Returns a list of dicts with: mint, symbol, name, market_cap,
        graduation_ts_ms, raydium_pool.

        Falls back to [] on any network/parse error — the tracker simply
        waits until the next cycle rather than propagating the exception.
        """
        cutoff_ms = (
            datetime.now(timezone.utc) - timedelta(minutes=LAUNCH_MAX_AGE_MINUTES)
        ).timestamp() * 1000

        for sort_key in ("last_trade_timestamp",):
            params = {
                "sort": sort_key,
                "order": "DESC",
                "limit": "50",
                "complete": "true",
                "includeNsfw": "false",
            }
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        PUMPFUN_API_URL, params=params, timeout=PUMPFUN_TIMEOUT
                    ) as resp:
                        if resp.status != 200:
                            self._last_fetch_problem = f"HTTP {resp.status} (sort={sort_key})"
                            logger.debug(
                                "pump.fun API sort=%s returned HTTP %d",
                                sort_key, resp.status,
                            )
                            continue
                        data = await resp.json(content_type=None)
            except Exception as e:
                # %r not %s: asyncio.TimeoutError and friends stringify to ""
                # which produced unreadable 'failed: ' log lines pre-2026-06-11.
                self._last_fetch_problem = repr(e)[:120]
                logger.warning("pump.fun API fetch (sort=%s) failed: %r", sort_key, e)
                continue

            if not isinstance(data, list):
                continue

            graduates = []
            for coin in data:
                if not coin.get("complete"):
                    continue
                # Prefer graduation_timestamp; fall back to king_of_the_hill_timestamp
                grad_ts = (
                    coin.get("graduation_timestamp")
                    or coin.get("king_of_the_hill_timestamp")
                )
                # Skip coins older than our window (timestamp is in milliseconds)
                if grad_ts and grad_ts < cutoff_ms:
                    continue
                mint = coin.get("mint", "")
                if not mint:
                    continue
                graduates.append({
                    "mint":             mint,
                    "symbol":           coin.get("symbol", ""),
                    "name":             coin.get("name", ""),
                    "graduation_ts_ms": grad_ts,
                    "market_cap":       float(coin.get("usd_market_cap") or 0),
                    "raydium_pool":     coin.get("raydium_pool"),
                })

            if graduates:
                return graduates

        return []

    # ─── Evaluation ────────────────────────────────────────────────────────────

    async def _evaluate_graduate(self, grad: dict, stats: dict) -> bool:
        """
        Apply entry filters. Returns True if a signal was fired.

        Filter order matters: cheapest / most-eliminating checks first.
        Market cap and liquidity are the primary gates; age and volume
        provide secondary confirmation.
        """
        mint   = grad["mint"]
        symbol = grad.get("symbol") or stats.get("base_token", mint[:8])

        # 1. Liquidity floor — must be tradeable post-graduation
        liquidity = float(stats.get("liquidity_usd") or 0)
        if liquidity < LAUNCH_MIN_LIQUIDITY_USD:
            logger.debug(
                "Launch %s: liquidity too low $%.0f (min $%.0f)",
                symbol, liquidity, LAUNCH_MIN_LIQUIDITY_USD,
            )
            return False

        # 2. Market cap ceiling — reject tokens that have already pumped hard
        market_cap = float(stats.get("market_cap") or grad.get("market_cap") or 0)
        if market_cap > LAUNCH_MAX_MARKET_CAP_USD:
            logger.debug(
                "Launch %s: market_cap too high $%.0f (max $%.0f)",
                symbol, market_cap, LAUNCH_MAX_MARKET_CAP_USD,
            )
            return False

        # 3. Volume floor — confirms real buying interest, not a ghost listing
        vol_1h = float(stats.get("volume_1h") or 0)
        if vol_1h < LAUNCH_MIN_VOLUME_USD:
            logger.debug(
                "Launch %s: volume_1h too low $%.0f (min $%.0f)",
                symbol, vol_1h, LAUNCH_MIN_VOLUME_USD,
            )
            return False

        # 4. Age check via Dexscreener pairCreatedAt (unix ms)
        #    Secondary to pump.fun's graduation_ts filter — catches cases where
        #    the pump.fun field is missing but Dexscreener has the pair timestamp.
        pair_created_ms = stats.get("pair_created_at")
        if pair_created_ms:
            age_min = (
                datetime.now(timezone.utc).timestamp() * 1000 - float(pair_created_ms)
            ) / 60_000
            if age_min > LAUNCH_MAX_AGE_MINUTES:
                logger.debug(
                    "Launch %s: pair too old %.1f min (max %d)",
                    symbol, age_min, LAUNCH_MAX_AGE_MINUTES,
                )
                return False
        else:
            age_min = None

        # 5. DB dedup — prevent double-firing on restarts or API duplicates
        if get_recent_launch_signal(mint, within_hours=2):
            return False

        await self._fire_launch_signal(grad, stats, age_min)
        return True

    # ─── Signal emission ───────────────────────────────────────────────────────

    async def _fire_launch_signal(self, grad: dict, stats: dict, age_min):
        mint       = grad["mint"]
        symbol     = grad.get("symbol") or stats.get("base_token", mint[:8])
        liquidity  = float(stats.get("liquidity_usd") or 0)
        market_cap = float(stats.get("market_cap") or grad.get("market_cap") or 0)
        vol_1h     = float(stats.get("volume_1h") or 0)
        price      = float(stats.get("price_usd") or 0)
        dex        = stats.get("dex", "unknown")

        logger.info(
            "LAUNCH SIGNAL: %s | dex=%s | liq=$%.0f | mcap=$%.0f | vol1h=$%.0f | age=%s",
            symbol, dex, liquidity, market_cap, vol_1h,
            f"{age_min:.1f}m" if age_min is not None else "?",
        )

        extra = {
            "graduation_ts_ms":  grad.get("graduation_ts_ms"),
            "age_at_signal_min": round(age_min, 1) if age_min is not None else None,
            "raydium_pool":      grad.get("raydium_pool"),
            "dex":               dex,
            "market_cap":        market_cap,
            "liquidity_usd":     liquidity,
            "volume_1h":         vol_1h,
            "price_usd":         price,
            "dexscreener_url":   stats.get("url"),
        }

        signal_id = create_signal(
            signal_type="launch",
            chain="solana",
            token_address=mint,
            token_symbol=symbol,
            trigger_count=1,
            extra_data=json.dumps(extra),
        )

        # Jupiter route check: tells us whether the token is actually tradeable
        # at signal time. Stored in signal_outcomes.route_available_at_signal.
        route_available = await check_route_available_solana(mint)

        try:
            record_signal_outcome(
                signal_id=signal_id,
                signal_type="launch",       # maps to launch_momentum (RESEARCH)
                token_address=mint,
                chain="solana",
                price_at_signal=price,
                route_available=route_available,
                token_symbol=symbol,
            )
        except Exception as e:
            logger.warning(
                "record_signal_outcome failed for launch signal %d: %s", signal_id, e
            )

        if self.on_signal:
            await self.on_signal({
                "signal_id":     signal_id,
                "type":          "launch",
                "chain":         "solana",
                "token_address": mint,
                "token_symbol":  symbol,
                "stats":         stats,
                "extra":         extra,
            })
