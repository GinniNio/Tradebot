"""
wallet_tracker.py — Smart Wallet Tracker

How it works:
  1. Loads tracked wallet addresses from the DB (smart_wallets table).
  2. Polls each wallet's recent transactions via Birdeye (Solana) or Dexscreener.
  3. Records every buy into wallet_activity.
  4. Checks if WALLET_TRIGGER_COUNT or more tracked wallets bought the same token
     within WALLET_LOOKBACK_MINUTES.
  5. If threshold met, creates a SIGNAL and fires the on_signal callback.

Adding new smart wallets:
  - Call wallet_tracker.add_wallet(address) at runtime.
  - Or insert directly into the smart_wallets table via DB.
  - The bot will also auto-discover wallets by watching who's early into winning trades.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Callable, Awaitable

from birdeye_client import BirdeyeClient
from dexscreener_client import DexscreenerClient
from helius_client import HeliusClient
from database import (
    get_smart_wallets, add_smart_wallet, record_wallet_activity,
    count_wallet_buys, get_hot_tokens, create_signal, update_wallet_last_seen,
)
from config import (
    WALLET_TRIGGER_COUNT, WALLET_POLL_INTERVAL_SEC,
    WALLET_LOOKBACK_MINUTES, MAX_TRACKED_WALLETS,
    SMART_WALLET_MIN_WIN_RATE, BIRDEYE_API_KEY, HELIUS_API_KEY,
)

logger = logging.getLogger(__name__)

# Type alias for signal callback
SignalCallback = Callable[[dict], Awaitable[None]]


class WalletTracker:
    """
    Polls tracked wallet activity and emits buy signals when enough
    smart wallets converge on the same token.
    """

    def __init__(self, on_signal: SignalCallback = None):
        self.on_signal = on_signal  # async callback(signal_dict)
        self._running = False
        # Track which (wallet, tx_hash) pairs we've already processed
        self._seen_txs: set[str] = set()
        # Track which token signals we've recently fired to avoid spam
        self._recent_signals: dict[str, datetime] = {}
        self._signal_cooldown_minutes = 30

    # ─── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        logger.info("WalletTracker starting...")
        self._running = True
        await self._run_loop()

    def stop(self):
        self._running = False

    # ─── Main loop ─────────────────────────────────────────────────────────────

    async def _run_loop(self):
        # Open both clients; either may be unused depending on which key is set
        async with BirdeyeClient() as birdeye, \
                   HeliusClient() as helius, \
                   DexscreenerClient() as dex:
            while self._running:
                try:
                    await self._poll_cycle(birdeye, helius, dex)
                except Exception as e:
                    logger.error("WalletTracker poll error: %s", e, exc_info=True)
                await asyncio.sleep(WALLET_POLL_INTERVAL_SEC)

    async def _poll_cycle(self, birdeye, helius, dex):
        wallets = get_smart_wallets()
        if not wallets:
            logger.debug("No tracked wallets yet. Add some via add_wallet().")
            return

        logger.debug("Polling %d tracked wallets...", len(wallets))
        # Semaphore=1 + sleep=0.6s inside each poll keeps Helius usage at
        # ~1.67 req/s — safely under the Enhanced API limit of 2 req/s.
        # The old Semaphore(5) + sleep(0.3s) fired ~16 req/s, causing
        # thousands of 429s per day with zero productive wallet signals.
        sem = asyncio.Semaphore(1)
        tasks = [self._poll_wallet(w, birdeye, helius, sem) for w in wallets]
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._check_triggers(dex)

    async def _poll_wallet(self, wallet, birdeye, helius, sem):
        """
        Provider selection (Solana only):
          1. Helius — preferred (free tier, parsed swap data)
          2. Birdeye — fallback if HELIUS_API_KEY missing but BIRDEYE_API_KEY set
          3. Skip + warn once if neither is available
        """
        async with sem:
            address = wallet["address"]
            chain = wallet.get("chain", "solana")
            try:
                if chain != "solana":
                    if not getattr(self, "_warned_non_solana", set()):
                        self._warned_non_solana = set()
                    if chain not in self._warned_non_solana:
                        logger.warning(
                            "Wallet polling for chain=%s not supported — wallet %s ignored.",
                            chain, address[:8],
                        )
                        self._warned_non_solana.add(chain)
                elif HELIUS_API_KEY:
                    txs = await helius.get_address_transactions(address, tx_type="SWAP", limit=20)
                    self._parse_helius_txs(address, chain, txs, helius)
                elif BIRDEYE_API_KEY:
                    txs = await birdeye.get_wallet_transactions(address, limit=20, chain=chain)
                    self._parse_birdeye_txs(address, chain, txs)
                else:
                    if not getattr(self, "_warned_no_keys", False):
                        logger.warning(
                            "Wallet %s tracked but neither HELIUS_API_KEY nor "
                            "BIRDEYE_API_KEY is set — Solana polling disabled.",
                            address[:8],
                        )
                        self._warned_no_keys = True

                update_wallet_last_seen(address)
                await asyncio.sleep(0.6)
            except Exception as e:
                logger.warning("Error polling wallet %s: %s", address[:8], e)

    def _parse_helius_txs(self, wallet: str, chain: str, txs: list, helius):
        """Parse Helius enhanced-tx list and record buys/sells into wallet_activity."""
        for tx in txs:
            normalized = helius.parse_swap_for_tracker(tx, wallet)
            if not normalized:
                continue
            tx_hash = normalized["tx_hash"]
            if not tx_hash or tx_hash in self._seen_txs:
                continue
            self._seen_txs.add(tx_hash)
            if len(self._seen_txs) > 50_000:
                self._seen_txs = set(list(self._seen_txs)[-25_000:])

            record_wallet_activity(
                wallet=wallet,
                chain=chain,
                token_address=normalized["token_address"],
                token_symbol=normalized["token_symbol"],
                action=normalized["side"],
                amount_usd=normalized["amount_usd"],
                tx_hash=tx_hash,
            )

    def _parse_birdeye_txs(self, wallet: str, chain: str, txs: list):
        """
        Parse Birdeye transaction list and record buys into DB.
        Birdeye tx items typically have: txHash, blockUnixTime, side ('buy'/'sell'),
        from/to token info, volumeUSD, etc.
        """
        for tx in txs:
            tx_hash = tx.get("txHash") or tx.get("signature", "")
            if not tx_hash or tx_hash in self._seen_txs:
                continue
            self._seen_txs.add(tx_hash)
            # Cap seen set size
            if len(self._seen_txs) > 50_000:
                self._seen_txs = set(list(self._seen_txs)[-25_000:])

            side = (tx.get("side") or tx.get("type") or "").lower()
            if side not in ("buy", "sell"):
                continue

            # Extract token address being bought
            # Birdeye structure varies; try common field names
            if side == "buy":
                token_addr = (tx.get("to", {}) or {}).get("address") or \
                             tx.get("toToken", {}).get("address") or \
                             tx.get("tokenAddress", "")
                token_sym  = (tx.get("to", {}) or {}).get("symbol") or \
                             tx.get("toToken", {}).get("symbol") or ""
            else:
                token_addr = (tx.get("from", {}) or {}).get("address") or \
                             tx.get("fromToken", {}).get("address") or \
                             tx.get("tokenAddress", "")
                token_sym  = (tx.get("from", {}) or {}).get("symbol") or \
                             tx.get("fromToken", {}).get("symbol") or ""

            if not token_addr:
                continue

            # Skip stablecoins and wrapped SOL
            SKIP_TOKENS = {
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
                "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
                "So11111111111111111111111111111111111111112",    # wSOL
            }
            if token_addr in SKIP_TOKENS:
                continue

            amount_usd = float(tx.get("volumeUSD") or tx.get("volume", 0) or 0)

            record_wallet_activity(
                wallet=wallet,
                chain=chain,
                token_address=token_addr,
                token_symbol=token_sym,
                action=side,
                amount_usd=amount_usd,
                tx_hash=tx_hash,
            )

    # ─── Signal detection ──────────────────────────────────────────────────────

    async def _check_triggers(self, dex: DexscreenerClient):
        """Look for tokens where >= WALLET_TRIGGER_COUNT wallets bought recently."""
        hot_tokens = get_hot_tokens(
            within_minutes=WALLET_LOOKBACK_MINUTES,
            min_wallet_count=WALLET_TRIGGER_COUNT,
        )

        for token in hot_tokens:
            token_addr = token["token_address"]
            wallet_count = token["wallet_count"]
            chain = token.get("chain", "solana")

            # Cooldown check — don't spam the same token signal
            last_signal = self._recent_signals.get(token_addr)
            if last_signal and datetime.utcnow() - last_signal < timedelta(minutes=self._signal_cooldown_minutes):
                continue

            # Get buying wallets detail
            buyers = count_wallet_buys(token_addr, within_minutes=WALLET_LOOKBACK_MINUTES)

            logger.info(
                "WALLET SIGNAL: %s | %d wallets buying | chain=%s",
                token.get("token_symbol", token_addr[:8]), wallet_count, chain
            )

            # Get live price/stats for context
            stats = await dex.get_token_stats(token_addr, preferred_chain=chain)

            extra = {
                "buying_wallets": [b["wallet"] for b in buyers],
                "wallet_count": wallet_count,
                "stats": stats,
            }

            signal_id = create_signal(
                signal_type="wallet",
                chain=chain,
                token_address=token_addr,
                token_symbol=token.get("token_symbol", ""),
                trigger_count=wallet_count,
                extra_data=json.dumps(extra),
            )

            self._recent_signals[token_addr] = datetime.utcnow()

            if self.on_signal:
                await self.on_signal({
                    "signal_id":    signal_id,
                    "type":         "wallet",
                    "chain":        chain,
                    "token_address":token_addr,
                    "token_symbol": token.get("token_symbol", ""),
                    "wallet_count": wallet_count,
                    "stats":        stats,
                    "buyers":       buyers,
                })

    # ─── Wallet management ─────────────────────────────────────────────────────

    def add_wallet(self, address: str, chain: str = "solana",
                   win_rate: float = 0.0, notes: str = "") -> bool:
        """Add a wallet to tracking at runtime."""
        wallets = get_smart_wallets()
        if len(wallets) >= MAX_TRACKED_WALLETS:
            logger.warning("Max tracked wallets (%d) reached.", MAX_TRACKED_WALLETS)
            return False
        added = add_smart_wallet(address, chain, win_rate, notes=notes)
        if added:
            logger.info("Added wallet to tracking: %s (%s)", address[:12], chain)
        return added

    async def score_and_add_wallet(self, address: str, chain: str = "solana") -> bool:
        """
        Fetch wallet stats from Birdeye, compute a win rate, and add if above threshold.
        Call this to auto-discover new smart wallets from early entries.
        """
        if not BIRDEYE_API_KEY:
            # Without Birdeye key, just add with default win_rate
            return self.add_wallet(address, chain)

        try:
            async with BirdeyeClient() as birdeye:
                gains = await birdeye.get_wallet_gains(address, chain)
                win_rate = float(gains.get("winRate", 0.0) or 0.0)
                trade_count = int(gains.get("tradeCount", 0) or 0)

            if win_rate >= SMART_WALLET_MIN_WIN_RATE:
                return self.add_wallet(address, chain, win_rate=win_rate,
                                       notes=f"Auto-added, win_rate={win_rate:.0%}")
            else:
                logger.debug("Wallet %s win_rate %.0f%% below threshold", address[:8], win_rate * 100)
                return False
        except Exception as e:
            logger.error("score_and_add_wallet error: %s", e)
            return False

    # ─── Seed wallets ──────────────────────────────────────────────────────────

    def seed_wallets(self, wallets: list[dict]):
        """
        Bulk-add wallets at startup.
        Each entry: {"address": "...", "chain": "solana", "win_rate": 0.75, "notes": "..."}
        """
        added = 0
        for w in wallets:
            if add_smart_wallet(
                address=w["address"],
                chain=w.get("chain", "solana"),
                win_rate=w.get("win_rate", 0.0),
                notes=w.get("notes", ""),
            ):
                added += 1
        logger.info("Seeded %d/%d wallets into tracker.", added, len(wallets))
