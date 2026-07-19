"""
prune_wallets.py - Suggest which tracked wallets to drop, watch, or keep.

Reads tradebot.db and scores each tracked wallet on:
  - last_seen recency (wallets the bot has never observed are inactive or invisible
    to your provider - the Helius parser may not match their swap structure)
  - contribution count (how many signals this wallet helped trigger)
  - signal win rate (when this wallet contributed, what's the P&L of those signals?)

Output: prune_report.txt + console summary with DROP / WATCH / KEEP per wallet.

Usage:
  python prune_wallets.py            # report only, no changes
  python prune_wallets.py --apply    # also delete DROP wallets from the DB

Run weekly after the bot has been collecting data for at least a few days.
"""

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import DB_PATH

# Decision thresholds (tune after you have more data)
DROP_IF_NEVER_SEEN_DAYS  = 14    # wallet has no recorded activity in this many days
DROP_IF_WIN_RATE_BELOW   = 30    # % win rate; needs MIN_SIGNALS_FOR_VERDICT to apply
KEEP_IF_WIN_RATE_ABOVE   = 60    # % win rate; needs MIN_SIGNALS_FOR_VERDICT
MIN_SIGNALS_FOR_VERDICT  = 3     # need this many signal contributions before judging


# ─── Data loading ─────────────────────────────────────────────────────────────

def load_data():
    if not Path(DB_PATH).exists():
        print(f"DB not found at {DB_PATH}. Has the bot ever run?")
        sys.exit(1)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    wallets = [dict(r) for r in con.execute(
        "SELECT * FROM smart_wallets ORDER BY added_at").fetchall()]
    signals = [dict(r) for r in con.execute(
        "SELECT * FROM signals WHERE signal_type='wallet'").fetchall()]
    trades = [dict(r) for r in con.execute(
        "SELECT * FROM trade_history").fetchall()]
    con.close()
    return wallets, signals, trades


def attribute_signals(signals: list[dict]) -> dict[str, list[tuple]]:
    """
    From each wallet signal's extra_data JSON, recover which wallets contributed.
    Returns: {wallet_address: [(signal_id, token_address), ...]}
    """
    contrib = defaultdict(list)
    for s in signals:
        if not s.get("extra_data"):
            continue
        try:
            extra = json.loads(s["extra_data"])
        except Exception:
            continue
        for w in extra.get("buying_wallets") or []:
            contrib[w].append((s["id"], s["token_address"]))
    return contrib


# ─── Scoring ──────────────────────────────────────────────────────────────────

def days_since(iso_ts: str | None) -> int:
    if not iso_ts:
        return 999
    try:
        ls = datetime.fromisoformat(iso_ts)
        if ls.tzinfo is None:
            ls = ls.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ls).days
    except Exception:
        return 999


def grade_wallet(w: dict, contrib: dict, trades_by_token: dict) -> dict:
    addr = w["address"]
    last_seen_days = days_since(w.get("last_seen"))
    wallet_contribs = contrib.get(addr, [])
    signal_count = len(wallet_contribs)

    # Win rate of signals this wallet helped trigger
    wins = losses = 0
    for sig_id, token_addr in wallet_contribs:
        for t in trades_by_token.get(token_addr, []):
            if (t.get("pnl_pct") or 0) > 0:
                wins += 1
            else:
                losses += 1
    total = wins + losses
    win_rate = (wins / total * 100) if total > 0 else None

    # Decision tree
    if last_seen_days > DROP_IF_NEVER_SEEN_DAYS:
        verdict = "DROP"
        reason = (f"never observed in {last_seen_days}d "
                  f"(inactive, or Helius parser misses their swap structure)")
    elif total >= MIN_SIGNALS_FOR_VERDICT and win_rate is not None and win_rate < DROP_IF_WIN_RATE_BELOW:
        verdict = "DROP"
        reason = f"{wins}/{total} signals won ({win_rate:.0f}%), below {DROP_IF_WIN_RATE_BELOW}%"
    elif total >= MIN_SIGNALS_FOR_VERDICT and win_rate is not None and win_rate >= KEEP_IF_WIN_RATE_ABOVE:
        verdict = "KEEP"
        reason = f"strong: {wins}/{total} signals won ({win_rate:.0f}%)"
    elif signal_count == 0 and last_seen_days <= DROP_IF_NEVER_SEEN_DAYS:
        verdict = "WATCH"
        reason = f"active ({last_seen_days}d) but hasn't fired a signal yet"
    else:
        verdict = "WATCH"
        wr_str = f"{win_rate:.0f}%" if win_rate is not None else "n/a"
        reason = (f"{signal_count} signal contribs, "
                  f"{wins}W/{losses}L ({wr_str}) - not enough data yet")

    return {
        "address":         addr,
        "last_seen_days":  last_seen_days,
        "signal_count":    signal_count,
        "wins":            wins,
        "losses":          losses,
        "win_rate":        win_rate,
        "verdict":         verdict,
        "reason":          reason,
        "notes":           w.get("notes") or "",
    }


# ─── Output ───────────────────────────────────────────────────────────────────

def format_line(g: dict) -> str:
    wr = f"{g['win_rate']:.0f}%" if g["win_rate"] is not None else "  -"
    note_suffix = f"  ({g['notes'][:30]})" if g["notes"] else ""
    return (
        f"[{g['verdict']:>5}] {g['address'][:16]}...  "
        f"last_seen={g['last_seen_days']:>3}d  "
        f"sigs={g['signal_count']:>2}  "
        f"W/L={g['wins']}/{g['losses']:<2}  "
        f"wr={wr:>4}  - {g['reason']}{note_suffix}"
    )


def apply_drops(drops: list[str]) -> int:
    """Delete DROP wallets from smart_wallets. Returns count deleted."""
    if not drops:
        return 0
    con = sqlite3.connect(DB_PATH)
    placeholders = ",".join("?" for _ in drops)
    cur = con.execute(
        f"DELETE FROM smart_wallets WHERE address IN ({placeholders})",
        drops,
    )
    con.commit()
    deleted = cur.rowcount
    con.close()
    return deleted


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Suggest wallet drops.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually delete DROP wallets from the DB.")
    args = parser.parse_args()

    wallets, signals, trades = load_data()
    if not wallets:
        print("No tracked wallets yet. Add some via the dashboard first.")
        return

    contrib = attribute_signals(signals)
    trades_by_token = defaultdict(list)
    for t in trades:
        trades_by_token[t["token_address"]].append(t)

    graded = [grade_wallet(w, contrib, trades_by_token) for w in wallets]
    graded.sort(key=lambda g: (g["verdict"], -g["signal_count"]))

    drops   = [g["address"] for g in graded if g["verdict"] == "DROP"]
    keeps   = [g["address"] for g in graded if g["verdict"] == "KEEP"]
    watches = [g["address"] for g in graded if g["verdict"] == "WATCH"]

    # Report file
    out_path = Path(__file__).parent / "prune_report.txt"
    now_str = datetime.now(timezone.utc).isoformat()
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"Wallet pruning report - {now_str}\n")
        f.write("=" * 80 + "\n\n")
        f.write(f"Total tracked:       {len(wallets)}\n")
        f.write(f"  DROP suggestions:  {len(drops)}\n")
        f.write(f"  KEEP confirmed:    {len(keeps)}\n")
        f.write(f"  WATCH (no call):   {len(watches)}\n\n")
        for g in graded:
            f.write(format_line(g) + "\n")

    # Console output
    print(f"\nWrote {out_path}\n")
    print(f"Total tracked: {len(wallets)}  |  "
          f"DROP: {len(drops)}  KEEP: {len(keeps)}  WATCH: {len(watches)}\n")
    for g in graded:
        print(format_line(g))

    if drops:
        print()
        if args.apply:
            n = apply_drops(drops)
            print(f"Deleted {n} wallets from smart_wallets (--apply).")
        else:
            print(f"To actually remove the {len(drops)} DROP wallet(s), re-run with --apply.")
            print("Or delete them from the dashboard one by one.")


if __name__ == "__main__":
    main()
