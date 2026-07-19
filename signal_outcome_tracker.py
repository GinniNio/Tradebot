"""
signal_outcome_tracker.py — Measurement layer (Phase 3).

Two responsibilities:

  1. record_signal_outcome(...) — called synchronously when a tracker fires
     a signal. Inserts a signal_outcomes row using already-fetched stats
     (no extra HTTP for price_at_signal) and enqueues four pending_price_checks
     at +5m, +15m, +1h, +24h.

  2. process_pending_checks_loop() — background coroutine. Wakes every
     CHECK_INTERVAL_SEC, drains due checks, batches Dexscreener calls by
     chain, writes the appropriate price_* column on each outcome row.

DB-backed scheduling: pending_price_checks survives bot restarts. A 24h
check enqueued at 09:00 today will fire tomorrow regardless of how many
times the bot has been restarted in between (which is frequently).

The tracker also captures Jupiter route availability at signal fire time
via a single get_quote call. RESEARCH-state signals get tracked even though
the state machine blocks the corresponding position from opening — that's
the whole point of RESEARCH.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from config import SIGNAL_TYPE_TO_STRATEGY, TRADE_SIZE_SOL
from database import (
    insert_signal_outcome, schedule_price_check,
    get_due_price_checks, complete_price_check, fail_price_check,
)
from dexscreener_client import DexscreenerClient
from telegram_notifier import notify_signal

logger = logging.getLogger(__name__)

# Background loop wake interval. Resolution of when due checks get fulfilled.
CHECK_INTERVAL_SEC = 60

# Post-signal snapshots to capture. Order matters only for log readability.
CHECK_SCHEDULE = [
    ("price_5m",  timedelta(minutes=5)),
    ("price_15m", timedelta(minutes=15)),
    ("price_1h",  timedelta(hours=1)),
    ("price_24h", timedelta(hours=24)),
    ("price_3d",  timedelta(days=3)),
    ("price_7d",  timedelta(days=7)),
]


# ─── Signal-time recording (sync from tracker) ────────────────────────────────

def record_signal_outcome(
    signal_id: int,
    signal_type: str,
    token_address: str,
    chain: str,
    price_at_signal: float,
    route_available: "bool | None" = None,
    token_symbol: str = "",
) -> "int | None":
    """
    Insert outcome row + enqueue all four post-signal price checks.

    Returns the new signal_outcome_id, or None if the insert failed.
    Idempotent on (signal_id, source='live') via UNIQUE constraint.

    `price_at_signal` should come from the stats dict that triggered the
    signal — do NOT make a fresh Dex call here, it's wasted I/O.

    `route_available` is the result of a Jupiter quote check. Pass None
    for non-Solana chains (Jupiter is Solana-only) — that's encoded as
    SQL NULL and excluded from route_rate aggregations.
    """
    strategy = SIGNAL_TYPE_TO_STRATEGY.get(signal_type, "zombie_revival")
    route_int = None if route_available is None else (1 if route_available else 0)

    outcome_id = insert_signal_outcome(
        signal_id=signal_id,
        strategy=strategy,
        token_address=token_address,
        chain=chain,
        price_at_signal=price_at_signal,
        route_available_at_signal=route_int,
        source="live",
    )
    if not outcome_id:
        logger.warning("signal_outcomes insert failed for signal_id=%d", signal_id)
        return None

    now = datetime.utcnow()
    for check_type, delta in CHECK_SCHEDULE:
        schedule_price_check(
            signal_outcome_id=outcome_id,
            token_address=token_address,
            chain=chain,
            check_type=check_type,
            due_at=(now + delta).isoformat(),
        )

    route_str = (
        "unknown" if route_available is None
        else ("yes" if route_available else "no")
    )
    logger.info(
        "Signal outcome created: strategy=%s token=%s checks=4 source=live "
        "signal_id=%d outcome_id=%d price=%.8f route=%s",
        strategy, token_address[:12], signal_id, outcome_id,
        price_at_signal or 0.0, route_str,
    )

    # Telegram push (no-op if unconfigured, fire-and-forget, never raises).
    # This is the single choke point every tracker flows through, so all
    # current and future signal sources get phone alerts for free.
    notify_signal(
        strategy=strategy,
        symbol=token_symbol or token_address[:8],
        token_address=token_address,
        chain=chain,
        price=price_at_signal or 0.0,
        route_available=route_available,
    )
    return outcome_id


async def check_route_available_solana(token_address: str) -> "bool | None":
    """
    Quick Jupiter route check at signal fire time. Returns True/False for
    Solana tokens, None on error. Caller is responsible for chain == 'solana'.

    JupiterClient is opened/closed per call — signals are rare (~3/day at
    current rate) so the connection-pool churn is irrelevant.
    """
    # Imported here, not at module top, to keep this module light if the
    # caller never invokes it (e.g. non-Solana chain).
    from jupiter_client import JupiterClient, SOL_MINT, LAMPORTS_PER_SOL
    try:
        async with JupiterClient() as jup:
            quote = await jup.get_quote(
                input_mint=SOL_MINT,
                output_mint=token_address,
                amount_lamports=int(TRADE_SIZE_SOL * LAMPORTS_PER_SOL),
            )
            return bool(quote and quote.get("outAmount"))
    except Exception as e:
        logger.debug("Route check failed for %s: %s", token_address[:12], e)
        return None


# ─── Background worker (async, runs in main.py task list) ─────────────────────

async def process_pending_checks_loop():
    """
    Forever-loop. Each cycle: pull due checks, group by chain, batch Dex,
    write back prices. Logs only when something happens — quiet at idle.
    """
    logger.info(
        "Signal outcome tracker: background loop starting (interval=%ds)",
        CHECK_INTERVAL_SEC,
    )
    async with DexscreenerClient() as dex:
        while True:
            try:
                await _process_one_cycle(dex)
            except Exception as e:
                logger.error(
                    "process_pending_checks cycle error: %s", e, exc_info=True,
                )
            await asyncio.sleep(CHECK_INTERVAL_SEC)


async def _process_one_cycle(dex: DexscreenerClient):
    due = get_due_price_checks(limit=200)
    if not due:
        return

    # Group by chain so the Dex batch endpoint can be used per chain.
    by_chain: dict[str, list[dict]] = {}
    for check in due:
        by_chain.setdefault(check["chain"], []).append(check)

    total_done = 0
    total_failed = 0

    for chain, checks in by_chain.items():
        addrs = list({c["token_address"] for c in checks})
        try:
            stats_map = await dex.get_token_stats_batch(addrs, preferred_chain=chain)
        except Exception as e:
            logger.warning(
                "signal_outcome batch fetch failed (chain=%s, n=%d): %s",
                chain, len(addrs), e,
            )
            continue

        for check in checks:
            stats = stats_map.get(check["token_address"])
            if not stats:
                fail_price_check(check["id"], "no_dex_pair")
                total_failed += 1
                logger.info(
                    "Signal outcome price check failed: token=%s check_type=%s "
                    "reason=no_dex_pair outcome_id=%d",
                    check["token_address"][:12], check["check_type"],
                    check["signal_outcome_id"],
                )
                continue
            price = stats.get("price_usd") or 0
            if price <= 0:
                fail_price_check(check["id"], "missing_price")
                total_failed += 1
                logger.info(
                    "Signal outcome price check failed: token=%s check_type=%s "
                    "reason=missing_price outcome_id=%d",
                    check["token_address"][:12], check["check_type"],
                    check["signal_outcome_id"],
                )
                continue
            try:
                complete_price_check(
                    check_id=check["id"],
                    price=price,
                    signal_outcome_id=check["signal_outcome_id"],
                    check_type=check["check_type"],
                )
                total_done += 1
                logger.info(
                    "Signal outcome price check completed: token=%s check_type=%s "
                    "price=%.8f outcome_id=%d",
                    check["token_address"][:12], check["check_type"], price,
                    check["signal_outcome_id"],
                )
            except Exception as e:
                fail_price_check(check["id"], f"write_error:{e}"[:200])
                total_failed += 1
                logger.warning(
                    "Signal outcome price check write error: token=%s check_type=%s "
                    "outcome_id=%d error=%s",
                    check["token_address"][:12], check["check_type"],
                    check["signal_outcome_id"], e,
                )

    if total_done or total_failed:
        logger.info(
            "Signal outcome tracker cycle: %d done, %d failed",
            total_done, total_failed,
        )
