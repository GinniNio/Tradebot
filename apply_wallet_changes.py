"""
apply_wallet_changes.py — One-off wallet set tune-up on 2026-05-21.

DROPS: J1sfMsbx (spd=3.3, sniper-tier) + FYTVwP5h (spd=3.2, sniper-tier)
ADDS:  4 quality candidates from today's 6am find_wallets run, all with
       moderate-to-slow trading velocity and no bot heuristic flags.

Idempotent — re-running this script after the changes are applied is a no-op.

Run:
    python apply_wallet_changes.py            # dry run, shows planned changes
    python apply_wallet_changes.py --apply    # actually writes to the DB
"""
import sqlite3
import sys
from datetime import datetime

DROPS = [
    ("J1sfMsbxGNXDPMUPXyGs5D6oCEe7fSYgdPMRyVzZuZUW", "spd=3.3 sniper-tier, drop"),
    ("FYTVwP5hgCUiB14eYYTPtZpBCBL4tqbYFbRkjmRwbNto", "spd=3.2 sniper-tier, drop"),
]

ADDS = [
    ("EKUkPpspEeqXwbUhq3LXDBH7o44MXsSfAMWeGFRbywCn",
     "fw-2026-05-21 score=36.0 age=526d spd=1.1 OG"),
    ("2ukMzNeFxhmQhrvekt4ETjjevBe6GiwiLkvXGEdkF45j",
     "fw-2026-05-21 score=25.7 age=50d spd=2.7"),
    ("5wsDAEQdWS61roSWE8wJiqYq4oiS2oPiqd7G96bRhVgN",
     "fw-2026-05-21 score=23.3 age=10d spd=2.3"),
    ("Bxhg23uytx17waoaKTqeS7BWQ8Lk489hJD7PQCJbwL8r",
     "fw-2026-05-21 score=20.7 age=50d spd=0.8 quiet"),
]

apply = "--apply" in sys.argv

conn = sqlite3.connect("tradebot.db")
conn.row_factory = sqlite3.Row

print("=" * 70)
print(f"Wallet set tune-up  ({'APPLY' if apply else 'DRY RUN'})")
print("=" * 70)
print()

# DROPS
print("DROPS:")
for addr, reason in DROPS:
    row = conn.execute(
        "SELECT id, trade_count FROM smart_wallets WHERE address = ?",
        (addr,),
    ).fetchone()
    if row is None:
        print(f"  - {addr[:12]}...  NOT FOUND (already removed?)  skip")
        continue
    print(f"  - {addr[:12]}...  id={row['id']}  trade_count={row['trade_count']}  ({reason})")
    if apply:
        conn.execute("DELETE FROM smart_wallets WHERE address = ?", (addr,))
        print(f"      DELETED")

print()
print("ADDS:")
now = datetime.utcnow().isoformat()
for addr, note in ADDS:
    row = conn.execute(
        "SELECT id FROM smart_wallets WHERE address = ?",
        (addr,),
    ).fetchone()
    if row is not None:
        print(f"  + {addr[:12]}...  ALREADY PRESENT (id={row['id']})  skip")
        continue
    print(f"  + {addr[:12]}...  {note}")
    if apply:
        conn.execute(
            "INSERT INTO smart_wallets (address, chain, win_rate, trade_count, added_at, notes) "
            "VALUES (?, 'solana', 0.0, 0, ?, ?)",
            (addr, now, note),
        )
        print(f"      INSERTED")

if apply:
    conn.commit()
    print()
    print("Committed.")
else:
    print()
    print("Dry run — no changes written. Re-run with --apply to commit.")

print()
print("Final wallet count:",
      conn.execute("SELECT COUNT(*) FROM smart_wallets").fetchone()[0])
conn.close()
