"""
main.py — Tradebot Orchestrator

Starts all modules concurrently:
  - WalletTracker  → monitors smart wallets, fires wallet signals
  - ZombieTracker  → monitors dormant tokens, fires zombie signals
  - Position Monitor → polls prices, enforces stop loss / take profit
  - Console Dashboard → prints P&L summary every 5 minutes

Usage:
  python main.py

Set PAPER_TRADING=False in .env only when you are ready to trade real funds.
"""

import asyncio
import logging
import signal
import sys
import time
from logging.handlers import RotatingFileHandler

from database import init_db, get_open_positions, close_position, open_position, mark_signal_acted
from wallet_tracker import WalletTracker
from zombie_tracker import ZombieTracker
from graduation_tracker import GraduationTracker
from swing_tracker import SwingTracker
from jupiter_client import JupiterClient
from risk_manager import RiskManager
from dexscreener_client import DexscreenerClient
from signal_outcome_tracker import process_pending_checks_loop
from config import (
    LOG_LEVEL, LOG_FILE, TRADE_SIZE_SOL, PAPER_TRADING,
    WALLET_POLL_INTERVAL_SEC, ZOMBIE_POLL_INTERVAL_SEC,
    WALLET_TRACKER_ENABLED,
    LAUNCH_TRACKER_ENABLED,
    SWING_TRACKER_ENABLED,
)

# SOL/USD price cache — refreshed every SOL_PRICE_CACHE_SEC
SOL_PRICE_CACHE_SEC = 300
SOL_MINT_ADDR = "So11111111111111111111111111111111111111112"

# ─── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging():
    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format=fmt)
    fh = RotatingFileHandler(LOG_FILE, maxBytes=10_000_000, backupCount=3)
    fh.setFormatter(logging.Formatter(fmt))
    logging.getLogger().addHandler(fh)

logger = logging.getLogger(__name__)


# ─── Tradebot core ─────────────────────────────────────────────────────────────

class Tradebot:
    def __init__(self):
        self.risk    = RiskManager()
        self.wallet_tracker = WalletTracker(on_signal=self.handle_signal)
        self.zombie_tracker = ZombieTracker(on_signal=self.handle_signal)
        self.graduation_tracker = GraduationTracker(on_signal=self.handle_signal)
        self.swing_tracker = SwingTracker(on_signal=self.handle_signal)
        self._running = False
        self._sol_price_cache: tuple[float, float] = (0.0, 150.0)  # (timestamp, price)

    # ─── Signal handler ────────────────────────────────────────────────────────

    async def handle_signal(self, signal_data: dict):
        """
        Called by WalletTracker or ZombieTracker when a buy signal fires.
        This is where the trade decision is made and executed.
        """
        sig_type    = signal_data["type"]          # 'wallet' or 'zombie'
        token_addr  = signal_data["token_address"]
        token_sym   = signal_data.get("token_symbol") or token_addr[:8]
        chain       = signal_data.get("chain", "solana")
        stats       = signal_data.get("stats", {})

        logger.info(
            "--- SIGNAL [%s] %s (%s) ---",
            sig_type.upper(), token_sym, chain
        )

        # Only execute Solana tokens via Jupiter for now
        if chain != "solana":
            logger.info(
                "Skipping %s — non-Solana (%s) execution not yet supported", token_sym, chain
            )
            return

        price_usd = stats.get("price_usd", 0.0) or 0.0
        if price_usd <= 0:
            logger.warning("No price data for %s — skipping", token_sym)
            return

        async with JupiterClient() as jup:
            sol_balance = await jup.get_sol_balance() if not PAPER_TRADING else 999.0

            # Ask risk manager for approval
            approved, reason = self.risk.can_open_position(
                token_address=token_addr,
                stats=stats,
                sol_balance=sol_balance,
                signal_type=sig_type,
            )
            if not approved:
                logger.info("Trade blocked: %s", reason)
                return

            sol_price_usd = await self._get_sol_price()
            pos_params = self.risk.compute_position(
                entry_price_usd=price_usd,
                sol_price_usd=sol_price_usd,
                signal_type=sig_type,
            )

            logger.info(
                "Opening %s position: size=%.3f SOL | SL=$%.6f | TP=$%.6f",
                sig_type, pos_params["trade_size_sol"],
                pos_params["stop_loss_usd"], pos_params["take_profit_usd"]
            )

            # Execute swap
            result = await jup.buy_token(
                token_mint=token_addr,
                sol_amount=pos_params["trade_size_sol"],
                sol_price_usd=sol_price_usd,
            )

            if not result.get("success"):
                logger.error("Swap failed: %s", result.get("error"))
                return

            # Token amount received (base units) — needed later for the sell
            quote = result.get("quote") or {}
            entry_token_amount = int(quote.get("outAmount") or result.get("out_amount") or 0)

            pos_id = open_position(
                chain=chain,
                token_address=token_addr,
                token_symbol=token_sym,
                entry_price_usd=price_usd,
                entry_amount_sol=pos_params["trade_size_sol"],
                entry_token_amount=entry_token_amount,
                size_usd=pos_params["size_usd"],
                stop_loss_usd=pos_params["stop_loss_usd"],
                take_profit_usd=pos_params["take_profit_usd"],
                signal_id=signal_data.get("signal_id"),
                signal_type=sig_type,
                tx_hash=result.get("tx_hash"),
            )
            if signal_data.get("signal_id"):
                mark_signal_acted(signal_data["signal_id"])

            logger.info(
                "Position #%d opened | %s | entry=$%.6f | tokens=%d | tx=%s",
                pos_id, token_sym, price_usd, entry_token_amount,
                (result.get("tx_hash") or "")[:20]
            )

    # ─── Price monitor ─────────────────────────────────────────────────────────

    async def monitor_positions(self):
        """
        Poll current prices for all open positions, decide exits, EXECUTE the sell
        via Jupiter (live mode) or simulate (paper mode), then close the position
        in the DB with the real exit price + tx.
        """
        async with DexscreenerClient() as dex:
            while self._running:
                try:
                    open_pos = get_open_positions()
                    if not open_pos:
                        await asyncio.sleep(30)
                        continue

                    # 1. Batch-fetch current prices, grouped by chain so each
                    # chain gets a single (or few-per-30) Dexscreener call
                    # instead of one HTTP per position.
                    price_map: dict = {}
                    by_chain: dict[str, list[dict]] = {}
                    for p in open_pos:
                        chain = p.get("chain") or "solana"
                        by_chain.setdefault(chain, []).append(p)

                    for chain, positions in by_chain.items():
                        addrs = [p["token_address"] for p in positions]
                        try:
                            stats_map = await dex.get_token_stats_batch(addrs, preferred_chain=chain)
                        except Exception as e:
                            logger.warning(
                                "Position price batch fetch failed (chain=%s, n=%d): %s",
                                chain, len(addrs), e,
                            )
                            continue
                        for p in positions:
                            s = stats_map.get(p["token_address"])
                            if s:
                                price_map[p["token_address"]] = s.get("price_usd", 0.0)

                    # 2. Ask risk manager which positions should exit
                    exits = self.risk.find_exit_candidates(price_map)
                    if not exits:
                        await asyncio.sleep(30)
                        continue

                    # 3. Execute each exit (sell on Jupiter, then close in DB)
                    async with JupiterClient() as jup:
                        for ex in exits:
                            await self._execute_exit(jup, ex)

                except Exception as e:
                    logger.error("Position monitor error: %s", e, exc_info=True)

                await asyncio.sleep(30)

    async def _execute_exit(self, jup: "JupiterClient", ex: dict):
        """
        Sell the held token via Jupiter (or paper-simulate), then record the close.
        """
        token_addr = ex["token_address"]
        token_sym  = ex["token_symbol"]
        amount     = ex["entry_token_amount"]
        reason     = ex["exit_reason"]
        exit_px    = ex["current_price"]

        # Only Solana exits are supported by Jupiter
        if ex.get("chain") and ex["chain"] != "solana":
            logger.warning(
                "Exit signal for non-Solana position %s — closing in DB only (no swap)",
                token_sym,
            )
            close_position(ex["position_id"], exit_px, reason, exit_tx=None)
            return

        if amount <= 0:
            # No token quantity recorded — can't size the sell. Close the record
            # so it doesn't hang forever, but flag the issue.
            logger.error(
                "Cannot sell %s: entry_token_amount is 0. Position %d will be closed "
                "in DB but token may still be held in wallet — sell manually.",
                token_sym, ex["position_id"],
            )
            close_position(ex["position_id"], exit_px, f"{reason}_no_qty", exit_tx=None)
            return

        logger.info(
            "EXIT [%s] %s | entry=$%.6f exit=$%.6f | PnL=%.1f%% | selling %d tokens",
            reason, token_sym, ex["entry_price_usd"], exit_px, ex["pnl_pct"], amount,
        )

        result = await jup.sell_token(token_mint=token_addr, token_amount=amount)
        if not result.get("success"):
            # Sell failed — DO NOT close the position. Try again next cycle.
            logger.error(
                "Sell failed for %s: %s — position %d stays open, will retry",
                token_sym, result.get("error"), ex["position_id"],
            )
            return

        close_position(ex["position_id"], exit_px, reason, exit_tx=result.get("tx_hash"))

    async def _get_sol_price(self) -> float:
        """
        Returns cached SOL/USD price; refreshes from Dexscreener every SOL_PRICE_CACHE_SEC.
        Falls back to last known price (or 150.0 on cold start) on error.
        """
        ts, cached = self._sol_price_cache
        now = time.time()
        if now - ts < SOL_PRICE_CACHE_SEC and cached > 0:
            return cached

        try:
            async with DexscreenerClient() as dex:
                stats = await dex.get_token_stats(SOL_MINT_ADDR, preferred_chain="solana")
                price = float(stats.get("price_usd") or 0) if stats else 0
                if price > 0:
                    self._sol_price_cache = (now, price)
                    return price
        except Exception as e:
            logger.warning("SOL price fetch failed: %s — using cached %.2f", e, cached)
        return cached or 150.0

    # ─── Dashboard ─────────────────────────────────────────────────────────────

    async def dashboard_loop(self):
        """Print P&L summary every 5 minutes."""
        while self._running:
            await asyncio.sleep(300)
            self.risk.print_summary()

    # ─── Startup ───────────────────────────────────────────────────────────────

    async def run(self):
        self._running = True
        mode = "PAPER TRADING" if PAPER_TRADING else "⚠️  LIVE TRADING"
        logger.info("=" * 60)
        logger.info("  TRADEBOT STARTING — %s", mode)
        logger.info("  Trade size: %.3f SOL per signal", TRADE_SIZE_SOL)
        logger.info("=" * 60)

        # Track child tasks so shutdown() can cancel them cleanly
        self._tasks = [
            asyncio.create_task(self.zombie_tracker.start(),     name="zombie_tracker"),
            asyncio.create_task(self.monitor_positions(),        name="monitor_positions"),
            asyncio.create_task(self.dashboard_loop(),           name="dashboard"),
            asyncio.create_task(process_pending_checks_loop(),   name="signal_outcomes"),
        ]
        if WALLET_TRACKER_ENABLED:
            self._tasks.append(
                asyncio.create_task(self.wallet_tracker.start(), name="wallet_tracker")
            )
        else:
            logger.warning(
                "WalletTracker DISABLED via WALLET_TRACKER_ENABLED=False. "
                "Zombie tracker still running. Re-enable in .env when ready."
            )
        if LAUNCH_TRACKER_ENABLED:
            self._tasks.append(
                asyncio.create_task(self.graduation_tracker.start(), name="graduation_tracker")
            )
        else:
            logger.warning(
                "GraduationTracker DISABLED via LAUNCH_TRACKER_ENABLED=False."
            )
        if SWING_TRACKER_ENABLED:
            self._tasks.append(
                asyncio.create_task(self.swing_tracker.start(), name="swing_tracker")
            )
        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass

    def shutdown(self):
        logger.info("Shutting down...")
        self._running = False
        self.wallet_tracker.stop()
        self.zombie_tracker.stop()
        self.graduation_tracker.stop()
        self.swing_tracker.stop()
        # Cancel running tasks so sleep() calls return immediately
        for t in getattr(self, "_tasks", []):
            if not t.done():
                t.cancel()


# ─── Entry point ───────────────────────────────────────────────────────────────

async def main():
    setup_logging()
    init_db()

    bot = Tradebot()

    # ── Optional: seed some known smart wallets at startup ──────────────────
    # bot.wallet_tracker.seed_wallets([
    #     {"address": "WALLET_ADDRESS_HERE", "chain": "solana", "win_rate": 0.72},
    # ])

    # ── Optional: manually add zombie candidates ─────────────────────────────
    # bot.zombie_tracker.add_token("solana", "TOKEN_MINT_HERE", "SYM")

    # Graceful shutdown on Ctrl+C: flip the flag, the run loops exit naturally.
    # Avoid loop.stop() — that triggers "Event loop stopped before Future completed".
    bot_task = asyncio.create_task(bot.run())

    def _request_shutdown():
        if bot._running:
            logger.info("Shutdown signal received")
            bot.shutdown()

    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, _request_shutdown)
        loop.add_signal_handler(signal.SIGTERM, _request_shutdown)
    except NotImplementedError:
        # Windows doesn't support add_signal_handler — fall back to default KeyboardInterrupt
        pass

    try:
        await bot_task
    except asyncio.CancelledError:
        pass
    finally:
        bot.risk.print_summary()
        logger.info("Tradebot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
