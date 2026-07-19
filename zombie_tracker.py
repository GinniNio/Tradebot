"""
zombie_tracker.py — Zombie Token Tracker

A "zombie" token is one that:
  - Was once active (had meaningful volume / price history)
  - Went dormant for ZOMBIE_DORMANT_DAYS or more (low/zero volume)
  - Is now showing signs of revival:
      * Volume spike >= ZOMBIE_VOLUME_MULTIPLIER × dormant average
      * Price up >= ZOMBIE_PRICE_CHANGE_PCT in the last hour
      * Liquidity >= ZOMBIE_MIN_LIQUIDITY_USD

How it works:
  1. Maintain a zombie_watchlist in the DB of dormant tokens.
  2. Every ZOMBIE_POLL_INTERVAL_SEC, check each watchlisted token via Dexscreener.
  3. If revival criteria met, fire a signal.
  4. Also periodically scan trending/new listings and add dormant ones to the watchlist.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Callable, Awaitable

from birdeye_client import BirdeyeClient
from dexscreener_client import DexscreenerClient
from database import (
    get_zombie_watchlist, add_to_zombie_watchlist, update_zombie_status,
    create_signal,
)
from signal_outcome_tracker import (
    record_signal_outcome, check_route_available_solana,
)
from config import (
    ZOMBIE_DORMANT_DAYS, ZOMBIE_VOLUME_MULTIPLIER, ZOMBIE_PRICE_CHANGE_PCT,
    ZOMBIE_POLL_INTERVAL_SEC, ZOMBIE_MIN_LIQUIDITY_USD, MONITORED_CHAINS,
    BIRDEYE_API_KEY, ZOMBIE_USE_BIRDEYE,
    ZOMBIE_V2_PC1H_MIN, ZOMBIE_V2_PC1H_MAX,
    ZOMBIE_V2_VOL1H_MAX_USD, ZOMBIE_V2_ALLOWED_CHAINS,
)

logger = logging.getLogger(__name__)

SignalCallback = Callable[[dict], Awaitable[None]]


class ZombieTracker:
    """
    Watches dormant tokens for sudden volume/price revivals.
    """

    def __init__(self, on_signal: SignalCallback = None):
        self.on_signal = on_signal
        self._running = False
        self._scan_counter = 0       # track cycles to throttle watchlist refresh

    # ─── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        logger.info("ZombieTracker starting...")
        self._running = True
        await self._run_loop()

    def stop(self):
        self._running = False

    # ─── Main loop ─────────────────────────────────────────────────────────────

    async def _run_loop(self):
        async with DexscreenerClient() as dex:
            # Birdeye is opt-in for the zombie loop. It's NOT enabled just because
            # a key is present — Birdeye Standard (1 RPS, 30k CU/mo) cannot sustain
            # per-token dormancy checks. Flip ZOMBIE_USE_BIRDEYE=True only if you
            # have a paid plan with the headroom.
            use_birdeye = ZOMBIE_USE_BIRDEYE and bool(BIRDEYE_API_KEY)
            birdeye_ctx = BirdeyeClient() if use_birdeye else None
            if birdeye_ctx:
                logger.info("ZombieTracker: Birdeye enabled for dormancy checks")
                async with birdeye_ctx as birdeye:
                    await self._inner_loop(dex, birdeye)
            else:
                logger.info("ZombieTracker: Dexscreener-only mode (Birdeye disabled)")
                await self._inner_loop(dex, None)

    async def _inner_loop(self, dex: DexscreenerClient, birdeye):
        while self._running:
            try:
                self._scan_counter += 1

                # Every 10 cycles (~10 min), refresh the watchlist with new dormant tokens
                if self._scan_counter % 10 == 1:
                    await self._refresh_watchlist(dex, birdeye)

                await self._check_watchlist(dex, birdeye)

            except Exception as e:
                logger.error("ZombieTracker error: %s", e, exc_info=True)

            await asyncio.sleep(ZOMBIE_POLL_INTERVAL_SEC)

    # ─── Watchlist refresh ─────────────────────────────────────────────────────

    async def _refresh_watchlist(self, dex: DexscreenerClient, birdeye):
        """
        Scan trending/new tokens and add dormant ones to the zombie watchlist.
        This is how we populate the watchlist over time.
        """
        logger.debug("ZombieTracker: refreshing watchlist...")
        added = 0

        # Approximations for dormant_since at add-time. These aren't precise
        # (we don't have the exact "last meaningfully active" timestamp from
        # either source) but they're better than NULL — gives the v2 forensic
        # a usable dormancy_days feature when this rolls forward.
        birdeye_dormant_since_iso = (
            datetime.utcnow() - timedelta(days=ZOMBIE_DORMANT_DAYS)
        ).isoformat()
        dexscreener_dormant_since_iso = (
            datetime.utcnow() - timedelta(days=1)  # vol_24h < $5k = at least 24h quiet
        ).isoformat()

        for chain in MONITORED_CHAINS:
            try:
                if birdeye and chain == "solana":
                    tokens = await birdeye.get_new_listings(chain=chain, limit=50)
                    for token in tokens:
                        addr = token.get("address") or token.get("tokenAddress", "")
                        sym  = token.get("symbol", "")
                        name = token.get("name", "")
                        if not addr:
                            continue
                        # Check if it's dormant via Birdeye volume history
                        is_dormant, avg_vol = await birdeye.is_token_dormant(
                            addr, dormant_days=ZOMBIE_DORMANT_DAYS, chain=chain
                        )
                        if is_dormant:
                            if add_to_zombie_watchlist(
                                chain, addr, sym, name, avg_vol,
                                last_active_at=birdeye_dormant_since_iso,
                            ):
                                added += 1
                else:
                    # Use Dexscreener trending to find candidates
                    trending = await dex.get_trending_across_chains(chains=[chain])
                    for item in trending[:30]:
                        addr = item.get("tokenAddress", "")
                        if not addr:
                            continue
                        stats = await dex.get_token_stats(addr, preferred_chain=chain)
                        if not stats:
                            continue
                        # Simple dormancy heuristic: very low volume last 24h
                        # We can't check full history without Birdeye, so use 24h vol
                        vol_24h = stats.get("volume_24h", 0)
                        if vol_24h < 5_000:
                            sym = stats.get("base_token", "")
                            if add_to_zombie_watchlist(
                                chain, addr, sym, "", vol_24h / 24,
                                last_active_at=dexscreener_dormant_since_iso,
                            ):
                                added += 1
                        await asyncio.sleep(0.2)

            except Exception as e:
                logger.warning("Watchlist refresh error for chain %s: %s", chain, e)

        if added:
            logger.info("ZombieTracker: added %d new tokens to watchlist", added)

    # ─── Watchlist check ───────────────────────────────────────────────────────

    async def _check_watchlist(self, dex: DexscreenerClient, birdeye):
        """
        Check all watching tokens for revival signs.

        Batched flow (post-Phase-2): one Dexscreener call per ~30 tokens per
        chain, then per-token revival check runs locally on pre-fetched stats.
        Previously this was one HTTP per token, which scaled badly as the
        watchlist grew.
        """
        watchlist = get_zombie_watchlist(status="watching")
        if not watchlist:
            return

        # Group by chain so we can use chain-preferred batch lookups.
        by_chain: dict[str, list[dict]] = {}
        for token in watchlist:
            chain = token.get("chain", "solana")
            by_chain.setdefault(chain, []).append(token)

        for chain, tokens in by_chain.items():
            addrs = [t["token_address"] for t in tokens]
            n_batches = (len(addrs) + 29) // 30  # ceil(len/30), matches Dex batch limit
            try:
                stats_map = await dex.get_token_stats_batch(addrs, preferred_chain=chain)
            except Exception as e:
                logger.warning(
                    "ZombieTracker batch fetch failed (chain=%s, n=%d): %s",
                    chain, len(addrs), e,
                )
                continue

            with_stats = 0
            for token in tokens:
                addr = token["token_address"]
                stats = stats_map.get(addr)
                if not stats:
                    continue
                with_stats += 1
                try:
                    await self._evaluate_token(token, stats, birdeye)
                except Exception as e:
                    logger.warning("check_token error for %s: %s", addr[:8], e)

            logger.info(
                "ZombieTracker check: chain=%s tokens=%d batches=%d with_stats=%d",
                chain, len(addrs), n_batches, with_stats,
            )

    async def _evaluate_token(self, token: dict, stats: dict, birdeye):
        """
        Run revival check on a single token using pre-fetched stats.

        The Birdeye dormancy refresh is preserved for the ZOMBIE_USE_BIRDEYE=True
        path (off by default — Birdeye Standard plan can't sustain it). When
        enabled, it overrides the DB-stored peak_volume_usd with a live
        14-day average for a more accurate dormant baseline.
        """
        addr  = token["token_address"]
        chain = token.get("chain", "solana")
        dormant_avg_vol = token.get("peak_volume_usd", 0.0) or 0.0

        if birdeye and chain == "solana":
            try:
                _is_dormant, avg_vol = await birdeye.is_token_dormant(
                    addr, dormant_days=ZOMBIE_DORMANT_DAYS, chain=chain,
                )
                dormant_avg_vol = avg_vol
            except Exception as e:
                logger.debug("Birdeye dormancy refresh failed for %s: %s", addr[:8], e)

        is_reviving = DexscreenerClient.revival_check_from_stats(
            stats,
            dormant_avg_volume=dormant_avg_vol,
            volume_multiplier=ZOMBIE_VOLUME_MULTIPLIER,
            min_price_change_pct=ZOMBIE_PRICE_CHANGE_PCT,
        )
        if not is_reviving:
            return

        liquidity = stats.get("liquidity_usd", 0)
        if liquidity < ZOMBIE_MIN_LIQUIDITY_USD:
            logger.debug(
                "Zombie %s reviving but liquidity too low: $%.0f",
                token.get("token_symbol", addr[:8]), liquidity,
            )
            return

        await self._fire_zombie_signal(token, stats)

    async def _fire_zombie_signal(self, token: dict, stats: dict):
        addr   = token["token_address"]
        symbol = token.get("token_symbol") or stats.get("base_token", addr[:8])
        chain  = token.get("chain", "solana")

        vol_1h = stats.get("volume_1h", 0) or 0
        pc_1h  = stats.get("price_change_1h", 0) or 0
        liq    = stats.get("liquidity_usd", 0) or 0

        # v2 selector — derived from 2026-05-28 winners-vs-losers forensic on
        # 41 v1 outcomes. ALL three must hold for v2 tagging. Outcomes are
        # logged under strategy='zombie_revival_v2' (RESEARCH) instead of
        # the v1 'zombie_revival' (STOP) so the two cohorts measure separately.
        # The signals row still uses signal_type='zombie' to satisfy the
        # existing CHECK constraint — the v1/v2 split lives downstream in
        # signal_outcomes.strategy.
        v2_passes = (
            ZOMBIE_V2_PC1H_MIN <= pc_1h <= ZOMBIE_V2_PC1H_MAX
            and vol_1h <= ZOMBIE_V2_VOL1H_MAX_USD
            and chain in ZOMBIE_V2_ALLOWED_CHAINS
        )
        outcome_signal_type = "zombie_v2" if v2_passes else "zombie"

        logger.info(
            "ZOMBIE SIGNAL [%s]: %s | 1h vol=$%.0f | 1h chg=%.1f%% | liq=$%.0f | chain=%s",
            "v2" if v2_passes else "v1",
            symbol, vol_1h, pc_1h, liq, chain,
        )

        # dormant_since may be NULL on older watchlist rows (pre-2026-05-28
        # bug — every caller of add_to_zombie_watchlist was passing the
        # default None). Fall back to added_at as a usable proxy. For rows
        # written after the bug fix this carries the real timestamp.
        dormant_since = token.get("dormant_since") or token.get("added_at")

        extra = {
            "selector_version":   "v2" if v2_passes else "v1",
            "dormant_since":      dormant_since,
            "revival_stats":      stats,
            "volume_1h":          vol_1h,
            "price_change_1h_pct":pc_1h,
            "liquidity_usd":      liq,
            "dexscreener_url":    stats.get("url"),
        }

        signal_id = create_signal(
            signal_type="zombie",  # CHECK constraint allows only 'wallet'|'zombie'
            chain=chain,
            token_address=addr,
            token_symbol=symbol,
            trigger_count=1,
            extra_data=json.dumps(extra),
        )

        # Record outcome row + enqueue post-signal price checks. v1/v2 split
        # is encoded here via signal_type ('zombie' vs 'zombie_v2'), which
        # signal_outcome_tracker translates into the strategy column.
        # price_at_signal reuses the already-fetched stats (no extra Dex call).
        price_at_signal = float(stats.get("price_usd") or 0)
        route_available = (
            await check_route_available_solana(addr) if chain == "solana" else None
        )
        try:
            record_signal_outcome(
                signal_id=signal_id,
                signal_type=outcome_signal_type,
                token_address=addr,
                chain=chain,
                price_at_signal=price_at_signal,
                route_available=route_available,
                token_symbol=symbol,
            )
        except Exception as e:
            logger.warning("record_signal_outcome failed for signal %d: %s",
                           signal_id, e)

        # Mark as triggered so we don't re-fire immediately
        update_zombie_status(addr, "triggered")

        if self.on_signal:
            await self.on_signal({
                "signal_id":       signal_id,
                "type":            "zombie",  # state gate sees this; v1 STOP, v2 RESEARCH — both block trades today
                "selector_version":"v2" if v2_passes else "v1",
                "chain":           chain,
                "token_address":   addr,
                "token_symbol":    symbol,
                "stats":           stats,
                "dormant_since":   dormant_since,
                "extra":           extra,
            })

    # ─── Manual watchlist management ───────────────────────────────────────────

    def add_token(self, chain: str, token_address: str, token_symbol: str = "",
                  name: str = "", peak_volume_usd: float = 0.0,
                  last_active_at: str = None) -> bool:
        """Manually add a token to the zombie watchlist.

        last_active_at: approximate ISO timestamp of when the token was last
        active. If None, defaults to "now minus ZOMBIE_DORMANT_DAYS" — assumes
        manual additions are at-least-dormant. Pass an explicit value if known.
        """
        if last_active_at is None:
            last_active_at = (
                datetime.utcnow() - timedelta(days=ZOMBIE_DORMANT_DAYS)
            ).isoformat()
        added = add_to_zombie_watchlist(
            chain, token_address, token_symbol, name, peak_volume_usd,
            last_active_at=last_active_at,
        )
        if added:
            logger.info("Manually added zombie candidate: %s (%s)", token_symbol, token_address[:12])
        return added

    async def seed_from_dexscreener(self, query: str, chain: str = "solana"):
        """
        Search Dexscreener for a token name/symbol and add dormant results to watchlist.
        Useful for seeding specific tokens you're watching.
        """
        dormant_since_iso = (
            datetime.utcnow() - timedelta(days=1)  # vol_24h < $5k = at least 24h quiet
        ).isoformat()
        async with DexscreenerClient() as dex:
            pairs = await dex.search_tokens(query)
            added = 0
            for pair in pairs:
                if pair.get("chainId", "").lower() != chain.lower():
                    continue
                vol = float(pair.get("volume", {}).get("h24", 0) or 0)
                if vol < 5_000:  # low volume = potentially dormant
                    addr = pair.get("baseToken", {}).get("address", "")
                    sym  = pair.get("baseToken", {}).get("symbol", "")
                    if addr and add_to_zombie_watchlist(
                        chain, addr, sym, "", vol / 24,
                        last_active_at=dormant_since_iso,
                    ):
                        added += 1
            logger.info("Seeded %d zombie candidates from search '%s'", added, query)
