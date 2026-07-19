"""dump_wallets.py — quick read of smart_wallets table for review."""
import sqlite3

conn = sqlite3.connect("tradebot.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT id, address, chain, win_rate, trade_count, added_at, last_seen, notes "
    "FROM smart_wallets ORDER BY id"
).fetchall()

print(f"{len(rows)} tracked wallets\n")
print(f"{'id':<3} {'address':<46} {'chain':<8} {'wr':>5} {'trades':>6} "
      f"{'added':<11} {'last_seen':<19} notes")
print("-" * 130)
for r in rows:
    d = dict(r)
    print(
        f"{d['id']:<3} {d['address']:<46} {d['chain']:<8} {d['win_rate']:>5.2f} "
        f"{d['trade_count']:>6} {(d.get('added_at') or '')[:10]:<11} "
        f"{(d.get('last_seen') or '')[:19]:<19} {(d.get('notes') or '')[:40]}"
    )
