# Handoff — 2026-05-21

Session focus: 24-hour signal audit, liquidity floor alignment, file truncation repair, Cowork project setup.

Bot status at end of session: **awaiting restart** to pick up the new config. After restart, paper trading resumes with the corrected zombie liquidity floor.

---

## What we did

### 1. Audited the first 24 hours of live signals

The DB showed 0 open positions and 0 closed trades, which initially looked like nothing fired. Reading `tradebot.log` line by line told a different story:

- **14 wallet signals** clustered May 20 between 10:47–10:55, opening positions #1–#12 across two restarts. The cluster came from 3-4 sniper bots crossing into different tokens within seconds of each other (the same correlation pattern we predicted before going live). At 10:55 the DB got wiped during a restart, taking positions #1–#12 with it.
- **3 zombie signals** on May 20: Cockcoin (12:48), MONSTERS (13:09), PERP (15:39). All three were rejected at trade time.
- **May 21**: zero new signals before the audit. The dashboard's "hot tokens" ribbon was showing live 3-wallet clusters, but trigger=6 was correctly filtering them.

### 2. Diagnosed the zombie rejection causes

| Token | Liquidity | Rejection |
|---|---|---|
| Cockcoin | $17,601 | Liquidity floor $20k > liq |
| MONSTERS | $13,980 | Liquidity floor $20k > liq |
| PERP | $21,842 | Jupiter "No routes found" |

The liquidity floor in `risk_manager.py` was $20k while `find_zombies.py` accepts down to $10k. Result: every zombie that barely qualified for the watchlist got vetoed at trade time. Misalignment.

### 3. Fixed the liquidity floor (set all three to $15k)

- `config.py` → `ZOMBIE_MIN_LIQUIDITY_USD = 15_000`
- `risk_manager.py` → `min_liq = 15_000 if signal_type == "zombie" else 10_000`
- `find_zombies.py` → `MIN_LIQUIDITY_USD = 15_000`

Replayed yesterday's signals against the new config:
- Cockcoin → would now PASS
- MONSTERS → still BLOCK (genuinely too thin)
- PERP → PASS risk, but Jupiter no-route still blocks (correct behavior — if we can't trade it for real, paper-trading would skew P&L)

### 4. Repaired truncated files

While verifying the config, discovered both `config.py` and `risk_manager.py` were truncated mid-file on disk (looked like earlier saves got cut off — possibly OneDrive sync conflicts or a partial Write). `risk_manager.py` would have raised `SyntaxError: '{' was never closed` on next bot restart. Rewrote both files in full via shell heredoc. Verified config loads cleanly and `RiskManager.can_open_position()` returns the expected verdicts.

### 5. Set up Cowork project context

Created `CLAUDE.md` at the project root. Future Cowork sessions opening this folder will read it automatically and skip the discovery phase. Covers: what the bot is, what's running 24/7, restart procedure, file map, current config, known gotchas, what NOT to do without asking.

Pinning the folder so it auto-opens in new chats is a Cowork UI step (the user will do this manually — pin/star icon in the folder selector).

---

## Decisions made

| Decision | Rationale |
|---|---|
| Liquidity floor = $15k (not $10k or $20k) | $20k was rejecting watchlist-eligible tokens. $10k allows tokens too thin for a clean exit. $15k matches the watchlist floor and gives a usable buffer. |
| Don't relax Jupiter "no routes" check | The current behavior (skip signal silently) is correct. Simulating trades we can't actually execute would corrupt the paper P&L baseline. |
| Don't rebuild the wallet set today | The current 8 wallets are correlated sniper bots but trigger=6 is muting them. Worth waiting for a few real zombie trades before re-doing wallet discovery — we need a comparison baseline. |
| Keep `PAPER_TRADING = True` | Need meaningful paper P&L (≥20 closed trades) before considering live mode. |
| Write CLAUDE.md as the onboarding doc | Faster than re-uploading context every session. References the existing `OPERATIONS_MANUAL.md` for depth. |

---

## State at end of session

- `config.py`: 68 lines, fully intact, $15k zombie floor
- `risk_manager.py`: 176 lines, fully intact, $15k zombie floor
- `find_zombies.py`: $15k minimum liquidity
- DB (`tradebot.db`): 0 open positions, 0 closed trades, 8 smart wallets, 20 zombie watchlist tokens, 3 unacted signals from May 20
- Scheduled tasks "Tradebot" and "find_wallets-daily" both still registered
- Dashboard on port 3001
- **Bot has NOT been restarted yet** — running process is still using the old config values. Restart needed before changes take effect.

---

## Work that remains

### Immediate (next session, first thing)

1. **Restart the bot to load the new config.**
   ```powershell
   Get-Process pythonw -ErrorAction SilentlyContinue | Stop-Process -Force
   Start-Sleep 3
   Start-ScheduledTask -TaskName "Tradebot"
   ```
   Then verify:
   ```powershell
   (Invoke-RestMethod http://localhost:3001/api/snapshot).stats
   Get-Content tradebot.log -Tail 20
   ```
   Look for fresh `TRADEBOT STARTING — PAPER TRADING` line.

2. **Verify the new liquidity floor is active.** After 1-2 hours of runtime, check if any zombie signals fired and whether they made it into `open_positions`. If a zombie with $15k–$20k liquidity still gets blocked, something didn't load.

### Short-term (this week)

3. **Watch for the first real zombie trade.** With the floor at $15k, tokens like Cockcoin would now go through. Tracking what happens to them (does the entry hold? does TP fire?) is the main signal we need.

4. **Check wallet tracker output.** If trigger=6 produces zero wallet signals over the next 48 hours despite the dashboard ribbon showing constant 3-wallet hits, that's evidence the sniper-bot population dominates the wallet set and we need to rebuild it.

### Medium-term (when 20+ paper trades have accumulated)

5. **Run `prune_wallets.py`** (read-only first, then `--apply`). Score the 8 current wallets by signal quality.

6. **Rebuild wallet set with tighter filters** if pruning leaves <4 useful wallets. Edit `find_wallets.py`: cross_token max = 3, bot_score penalty stronger, prefer wallets that aren't in the trending-token hot lists (would require a new `find_early_wallets.py` variant).

7. **Review zombie outcomes.** Are the picks running or fading? If win rate < 30% after 10 zombie trades, tighten the find_zombies filters (raise `MIN_VOLUME_SPIKE`, raise `MIN_PRICE_CHANGE_1H`).

### Long-term (only after paper P&L is positive over ≥50 trades)

8. **Go-live review.** Code-review `jupiter_client.py`'s live path, set `SOLANA_PRIVATE_KEY` in `.env`, flip `PAPER_TRADING = False`, start with `TRADE_SIZE_SOL = 0.01` (10x smaller than paper).

---

## Files touched this session

- `config.py` — restored full content, set `ZOMBIE_MIN_LIQUIDITY_USD = 15_000`
- `risk_manager.py` — restored full content, set zombie floor `min_liq = 15_000`
- `find_zombies.py` — `MIN_LIQUIDITY_USD = 15_000`
- `CLAUDE.md` — created (project briefing for Claude sessions)
- `docs/handoffs/2026-05-21-signal-audit-and-liquidity-alignment.md` — this file

## Files NOT touched (worth knowing)

- `tradebot.db` — left alone, 12,861 wallet activity rows preserved
- `tradebot.log` — left alone, full history
- `.env` — left alone, Helius key still working

---

## Gotchas the next session needs to remember

- **OneDrive sync conflicts can truncate files.** If a file looks like it has a `SyntaxError` at the bottom, check the last few lines with `wc -l` and `tail`. If it's been cut off mid-block, restore via shell heredoc rather than file-tool Edit (Edit will refuse if it can't match `old_string`).
- **The bash mount lags behind file-tool writes** by several seconds. After a Write, wait before running Python against the file in the sandbox.
- **Hot-tokens ribbon ≠ signals.** The dashboard shows 3-wallet clusters as "hot" for diagnostic purposes; only ≥6 wallets becomes an actual signal. Don't confuse the two.
- **Don't manually clear `tradebot.db`.** History is irreplaceable. If a wipe is genuinely needed, dump the closed-trade history to CSV first.

---

## Quick health check for the next session

```powershell
# 1. Is the bot up?
Get-Process pythonw -ErrorAction SilentlyContinue
(Invoke-RestMethod http://localhost:3001/api/snapshot).stats

# 2. What did it do overnight?
Get-Content C:\Users\hp\OneDrive\Documents\Claude\Projects\Tradebot\tradebot.log -Tail 100 |
  Select-String "SIGNAL|Position #|EXIT|ERROR"

# 3. Any new positions or closes?
python -c "import sqlite3; c=sqlite3.connect('tradebot.db'); print('open:', c.execute('SELECT COUNT(*) FROM open_positions').fetchone()[0]); print('closed:', c.execute('SELECT COUNT(*) FROM trade_history').fetchone()[0])"
```

If all three are healthy → continue with item 3 (watch for first real zombie trade). If bot is down → restart per item 1 above.
