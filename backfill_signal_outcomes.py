"""
backfill_signal_outcomes.py — One-shot historical backfill.

Creates source='backfill' rows in signal_outcomes for every existing row in
the signals table, giving the verdict query a coarse starting cohort instead
of waiting 10-14 days for live data to mature.

Rules (per the Phase 4 spec):
  - source='backfill' on every row written here
  - NO pending_price_checks are enqueued (history is fixed; no live work)
  - NO current Dexscreener price is forced into price_24h — that would
    contaminate the field's meaning. price_24h is filled ONLY when a closed
    trade exists and its (closed_at - opened_at) duration is between 12-36h.
  - route_available_at_signal = 1 only when a trade actually opened (proves
    a route existed); NULL otherwise (unknown — could be no route, could be
    state-gated, can't disambiguate from history)
  - price_at_signal pulled from signals.extra_data JSON, with fallback to
    matching trade's entry_price_usd
  - Idempotent: re-running skips signals that already have a backfill row
    (enforced by the UNIQUE(signal_id, source) constraint)

These rows are NEVER counted in /api/strategy_health (that query is scoped
to source='live'). Backfill is for STOP / keep-in-RESEARCH decisions only,
never promotion.

Usage:
  python backfill_signal_outcomes.py            # dry-run (no writes)
  python backfill_signal_outcomes.py --apply    # actually insert
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta

from database import get_conn, insert_signal_outcome
from config import SIGNAL_TYPE_TO_STRATEGY

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("backfill")

# How close (in seconds) the trade's opened_at must be to the signal's
# created_at to count as a match. Signals that result in trades fire within
# a few seconds; 5 minutes is generous.
TRADE_MATCH_WINDOW_SEC = 300

# Window (in hours) around 24h within which an exit timestamp is "close
# enough to 24h" to treat exit_price_usd as a price_24h proxy. Tight enough
# that stopped-out trades (minutes after entry) don't pollute the field.
PRICE_24H_LOWER_HOURS = 12
PRICE_24H_UPPER_HOURS = 36


def parse_iso(ts: str) -> "datetime | None":
    if not ts:
        return None
    try:
        # Strip 'Z' if present, handle microseconds either way
        return datetime.fromisoformat(ts.replace("Z", "+00:00").replace("+00:00", ""))
    except Exception:
        return None


def extract_signal_price(extra_data_json: str) -> "float | None":
    """Pull price_usd from a signal's extra_data JSON. Handles both the
    zombie shape (revival_stats.price_usd) and the wallet shape (stats.price_usd)."""
    if not extra_data_json:
        return None
    try:
        extra = json.loads(extra_data_json)
    except Exception:
        return None
    for path in (("revival_stats", "price_usd"), ("stats", "price_usd")):
        node = extra
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if isinstance(node, (int, float)) and node > 0:
            return float(node)
    return None


def find_matching_trade(conn, signal: dict) -> "dict | None":
    """
    Find a trade_history row that likely corresponds to this signal.
    Match by token_address + opened_at near signal.created_at.

    trade_history doesn't carry signal_id (pre-existing schema gap), so we
    match on the next-best heuristic.
    """
    sig_time = parse_iso(signal["created_at"])
    if not sig_time:
        return None
    lower = (sig_time - timedelta(seconds=TRADE_MATCH_WINDOW_SEC)).isoformat()
    upper = (sig_time + timedelta(seconds=TRADE_MATCH_WINDOW_SEC)).isoformat()
    row = conn.execute(
        """SELECT * FROM trade_history
           WHERE token_address = ?
             AND opened_at >= ?
             AND opened_at <= ?
           ORDER BY opened_at
           LIMIT 1""",
        (signal["token_address"], lower, upper),
    ).fetchone()
    return dict(row) if row else None


def derive_outcome_fields(signal: dict, trade: "dict | None") -> dict:
    """
    Compute price_at_signal, price_24h, route_available_at_signal from
    whatever historical data we have. Conservative — leaves fields NULL
    when uncertain rather than fabricating.
    """
    price_at_signal = extract_signal_price(signal.get("extra_data"))
    price_24h = None
    route_available = None

    if trade:
        # A trade opened means a route existed at signal time.
        route_available = 1
        if price_at_signal is None and trade.get("entry_price_usd"):
            price_at_signal = float(trade["entry_price_usd"])

        # Only fill price_24h if the exit happened in the 12-36h window
        # around 24h post-entry. Stopped-out trades exit in minutes and
        # don't qualify.
        opened = parse_iso(trade.get("opened_at"))
        closed = parse_iso(trade.get("closed_at"))
        if opened and closed and trade.get("exit_price_usd"):
            hours_held = (closed - opened).total_seconds() / 3600.0
            if PRICE_24H_LOWER_HOURS <= hours_held <= PRICE_24H_UPPER_HOURS:
                price_24h = float(trade["exit_price_usd"])

    return {
        "price_at_signal": price_at_signal,
        "price_24h": price_24h,
        "route_available_at_signal": route_available,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually insert rows (default: dry-run)")
    args = ap.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    logger.info("Backfill starting (%s)", mode)

    with get_conn() as conn:
        signals = conn.execute(
            "SELECT * FROM signals ORDER BY created_at"
        ).fetchall()
        existing_backfill = {
            row["signal_id"] for row in conn.execute(
                "SELECT signal_id FROM signal_outcomes WHERE source = 'backfill'"
            ).fetchall()
        }
        logger.info(
            "Found %d historical signals; %d already backfilled",
            len(signals), len(existing_backfill),
        )

        # Counters
        inserted = 0
        skipped_existing = 0
        skipped_no_price = 0
        with_trade = 0
        with_price_24h = 0

        for sig_row in signals:
            sig = dict(sig_row)
            if sig["id"] in existing_backfill:
                skipped_existing += 1
                continue

            trade = find_matching_trade(conn, sig)
            if trade:
                with_trade += 1

            fields = derive_outcome_fields(sig, trade)

            # Without price_at_signal there's nothing useful to record.
            if fields["price_at_signal"] is None:
                skipped_no_price += 1
                logger.debug(
                    "Skipping signal_id=%d (%s): no recoverable price",
                    sig["id"], sig.get("token_symbol") or sig["token_address"][:8],
                )
                continue

            if fields["price_24h"] is not None:
                with_price_24h += 1

            strategy = SIGNAL_TYPE_TO_STRATEGY.get(
                sig["signal_type"], "zombie_revival",
            )

            if args.apply:
                outcome_id = insert_signal_outcome(
                    signal_id=sig["id"],
                    strategy=strategy,
                    token_address=sig["token_address"],
                    chain=sig["chain"],
                    price_at_signal=fields["price_at_signal"],
                    route_available_at_signal=fields["route_available_at_signal"],
                    source="backfill",
                )
                # If we have price_24h, set it directly (no pending check
                # needed — the data is already historical).
                if outcome_id and fields["price_24h"] is not None:
                    with get_conn() as upd:
                        upd.execute(
                            "UPDATE signal_outcomes SET price_24h = ? WHERE id = ?",
                            (fields["price_24h"], outcome_id),
                        )
                inserted += 1
            else:
                logger.info(
                    "[DRY-RUN] would insert: signal_id=%d strategy=%s "
                    "token=%s price_at_signal=%s price_24h=%s route=%s",
                    sig["id"], strategy,
                    (sig.get("token_symbol") or sig["token_address"][:8]),
                    f"{fields['price_at_signal']:.8f}",
                    "None" if fields["price_24h"] is None
                    else f"{fields['price_24h']:.8f}",
                    "1" if fields["route_available_at_signal"] == 1 else "NULL",
                )
                inserted += 1  # for counter purposes

    logger.info("=" * 60)
    logger.info("Backfill summary (%s):", mode)
    logger.info("  signals total           : %d", len(signals))
    logger.info("  already backfilled      : %d (skipped)", skipped_existing)
    logger.info("  no recoverable price    : %d (skipped)", skipped_no_price)
    logger.info("  with matching trade     : %d", with_trade)
    logger.info("  with price_24h filled   : %d (rest stay NULL)", with_price_24h)
    logger.info("  %s: %d rows", "INSERTED" if args.apply else "WOULD INSERT", inserted)
    if not args.apply:
        logger.info("Re-run with --apply to actually write.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
