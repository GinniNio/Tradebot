# Handoff — 2026-05-21 (evening session)

Session focus: first-principles teardown of the bot's trade structure, commitment to rebuild around an options-portfolio model, Phase 1 of the rebuild (DB schema + per-strategy config plumbing), universal graduation filter, watchdog + crash capture, wallet set tune-up.

Bot status at end of session: **running, paper-trading, healthy** (restarted at 17:01). New 10-wallet set live. Graduation filter live. Watchdog running every 5 minutes. Daily 7am morning report scheduled.

---

## The architectural commitment (biggest move of the session)

Decided to rebuild the bot's trade structure around an **options-portfolio model** rather than the equity-style momentum-trader model it was originally designed as. Saved to project memory under `tradebot_options_portfolio_rebuild.md` and indexed in `MEMORY.md`.

The reasoning, in short. Memecoins have no cash flows and exhibit power-law returns (0.0002% of pump.fun tokens sustain >$1M cap, 0.5-1.4% even graduate from the bonding curve). They behave like long-dated call options on attention. The original bot used equity conventions (-15% stop, +50% TP, 10 positions × 0.1 SOL each) that mathematically cap the only payoffs that justify being in this market while paying full downside on losers. EV math at default settings requires >27% win rate to break even after fees, which memecoin retail at our tech tier rarely clears.

The rebuild specs (all confirmed by Kaye via AskUserQuestion):
- Position sizing: 0.02 SOL × max 40 positions = 0.8 SOL exposure, 4-5x the optionality of the v1 0.1 × 10 structure
- **No traditional stop loss on v2 positions.** The position size IS the option premium. Let losers go to zero.
- Tiered TPs: 20% off at +100%, 20% off at +300%, remaining 60% trails 30% from peak with no upper cap
- Selectivity over signal volume; filter on verifiable structural facts (LP lock, graduation, holder distribution, deployer behavior), not on price/volume patterns
- 30-second poll cadence stays. We're not in the sniper-speed game (sub-100ms is table stakes; we're 300x slower) and never will be
- v1 momentum logic stays running in parallel as a control, tagged `v1-momentum-baseline`

Two more memory files written this session that future Claudes should respect:
- `tradebot_base_rates.md` — empirical priors (graduation 0.63%, MEV 3%, holder bundles 36.5%, etc.). Sanity-check every threshold against these before changing it.
- `tradebot_objective.md` — short-term value buys on Solana memecoins; current strategies are momentum-chase; earlier-entry variants gated on ≥20 paper trades.

---

## What we shipped

### 1. Phase 1: DB schema + per-strategy config plumbing

Idempotent migrations added in `database._apply_migrations()`:
- `signals.strategy_tag` TEXT DEFAULT 'v1-momentum-baseline'
- `open_positions.strategy_tag` TEXT DEFAULT 'v1-momentum-baseline'
- `trade_history.strategy_tag` TEXT DEFAULT 'v1-momentum-baseline'
- `open_positions.tp_tier_state` TEXT — JSON list of TP tiers already fired, NULL for v1
- `open_positions.peak_price_usd` REAL — high-water mark for v2 trailing stop, NULL for v1
- `open_positions.remaining_token_amount` INTEGER — decrements as v2 TP tiers fire, NULL for v1

Helpers updated to accept the new columns: `create_signal()`, `open_position()`, `close_position()`. Migration tested against a tmp DB clone before deploy; tested as idempotent (re-running ALTER TABLE catches "duplicate column" gracefully).

`config.py` gained a `STRATEGIES` dict with per-strategy blocks. `v1-momentum-baseline` mirrors the original settings; `v2-options` has the new sizing/exit profile. Helper: `get_strategy_config(strategy_tag)`. Top-level constants (`TRADE_SIZE_SOL` etc.) preserved as backward-compat for code paths not yet migrated.

### 2. Phase 0.5: Universal graduation filter

Added to `risk_manager.can_open_position()`. Rejects any token whose primary pair is on a known bonding-curve venue. Currently blocks `pumpfun`, `four-meme`, `fourmeme`. Tokens on `pumpswap`, `raydium`, `orca`, `meteora` pass. Toggleable via `config.ENFORCE_GRADUATION_FILTER`.

Defensible per academic data: 0.5%-1.4% of Pump.fun tokens graduate to a real DEX. The remaining 98.6%-99.5% die on the bonding curve. Trading pre-graduation tokens means buying into a 99%-mortality sub-population. The filter applies to all strategies, not just v2 — it's a quality gate, not a strategy variant.

Tested against 9 scenarios (pumpfun blocked, pumpswap/raydium/orca/meteora pass, case-insensitive matches, missing dex field doesn't block, low-liq still rejected, toggle-off bypasses, etc.). All green.

### 3. Crash capture in run_all.py

`pythonw.exe` has no stdout/stderr by design. The original `run_all.py` redirected both to `/dev/null` to prevent print() crashes, which also silently swallowed every unhandled exception. Now:
- stdout/stderr redirect to `crash.log` (line-buffered, append mode)
- A session-start marker is written each time pythonw spins up
- A top-level `try/except BaseException` around `asyncio.run(main())` writes a clearly-marked `=== UNHANDLED CRASH === ... === end crash ===` block to both `crash.log` and the rotating `tradebot.log` before re-raising
- `KeyboardInterrupt` and `SystemExit` are passed through cleanly

Side effect to know about: `crash.log` will accumulate normal startup banner output (the `print()` calls in `run_all.py` that draw the box). Grep for `UNHANDLED CRASH` to find real crashes.

### 4. Watchdog scheduled task

New file `watchdog.ps1` and a registered scheduled task `Tradebot-watchdog`. Runs every 5 minutes. Checks:
- Is pythonw.exe running?
- Is `tradebot.log` mtime within the last 10 minutes?

If either fails, kills any stuck pythonw and restarts via `Start-Process pythonw.exe -ArgumentList "run_all.py" -WindowStyle Hidden`. Logs every check to `watchdog.log` (one line per check on happy path, expanded when intervening).

Registration command is in CLAUDE.md's restart procedure section (one-liner that constructs trigger + action + settings). First check at 16:59:30 reported `OK pid=5124 log_age=1.8` — clean happy-path verification.

Eliminates the "PC slept overnight, scheduled task triggers At Logon, no logon = no restart" failure mode we observed last night (19-hour blackout).

### 5. Daily 7am morning report

Created via `mcp__scheduled-tasks__create_scheduled_task` as task `tradebot-morning-report`. Fires daily at 7am local time. Generates a tight punch-list: bot uptime, last-24h signals split by strategy_tag, opens/closes + PnL, errors, signal block reasons (including graduation filter rejections), dashboard health.

Prompt is self-contained: future runs don't have memory of this conversation, so the prompt explicitly includes the output style preferences, the file paths and SQL queries to run, and the format rules. If everything's healthy, one line total. If something needs attention, lead with the issue.

The user should click "Run now" once from the Scheduled sidebar to pre-approve the tools (bash, web_fetch, file reads) so future 7am runs don't sit waiting on permission prompts.

### 6. Wallet set tune-up: 8 → 10 wallets

Compared the existing 8 against this morning's 6am `find_wallets.py` candidates (already ran via the scheduled task; output in `candidates.csv`).

**Dropped** (sniper-tier, spd > 3 swaps/day, no offsetting age signal):
- `J1sfMsbxGNXDPMUPXyGs5D6oCEe7fSYgdPMRyVzZuZUW` (spd=3.3)
- `FYTVwP5hgCUiB14eYYTPtZpBCBL4tqbYFbRkjmRwbNto` (spd=3.2)

**Added** (high score, no bot signature, meaningful age):
- `EKUkPpspEeqXwbUhq3LXDBH7o44MXsSfAMWeGFRbywCn` — score 36.0, 526d old, 1.1 swaps/day (OG)
- `2ukMzNeFxhmQhrvekt4ETjjevBe6GiwiLkvXGEdkF45j` — score 25.7, 50d old, 2.7 swaps/day
- `5wsDAEQdWS61roSWE8wJiqYq4oiS2oPiqd7G96bRhVgN` — score 23.3, 10d old, 2.3 swaps/day
- `Bxhg23uytx17waoaKTqeS7BWQ8Lk489hJD7PQCJbwL8r` — score 20.7, 50d old, 0.8 swaps/day (quiet)

Net: 10 wallets, 2 still sniper-tier (kept some for genuine cluster signals), 4 fresh quality picks, retained 4 of the original slow/medium-velocity wallets. Tooling: `dump_wallets.py` (inspect) + `apply_wallet_changes.py` (idempotent dry-run-then-apply).

### 7. CLAUDE.md correction

Original CLAUDE.md said the scheduled tasks were named `"Tradebot"` and `"find_wallets-daily"`. Actual names are `Tradebot-bot-always-on` and `Tradebot-find-wallets-daily`. This bit us today when `Start-ScheduledTask -TaskName "Tradebot"` failed. Fixed; added a discovery one-liner (`Get-ScheduledTask -TaskName "*Tradebot*"`) and the manual-launch fallback so the next reader doesn't repeat the dance.

---

## State at end of session

- `tradebot.db`: 0 open positions, 0 closed trades, 10 smart wallets, 22+ zombie watchlist tokens, 3 historical signals (pre-Phase-1)
- Bot: running on the PID from the 17:01:04 restart, all migrations applied
- Watchdog: running, first check OK at 16:59:30
- Daily report: scheduled for 7am tomorrow (next run in <14 hours)
- `crash.log`: 1.4KB, contains startup banners only (no actual crashes recorded)
- `watchdog.log`: 50 bytes, one OK check
- All 5 new DB columns migrated cleanly
- v1 still tagged as `v1-momentum-baseline` by default; nothing v2 is firing yet

---

## Decisions made (record so future sessions don't relitigate)

| Decision | Rationale |
|---|---|
| Commit to options-portfolio rebuild | Math says current bot is sub-breakeven (needs >27% win rate; memecoin retail rarely clears that). Power-law payoffs need power-law exit logic. |
| Keep v1 running as `v1-momentum-baseline` control | Lets us measure the rebuild empirically. Costs nothing (paper). |
| Position sizing v2 = 0.02 SOL × 40 max | Same exposure (0.8 SOL) with 4x the optionality. Paper balance is 999 SOL hardcoded so capacity isn't a constraint. |
| No traditional stop loss on v2 | Position size IS the option premium. Stops at -15% lock in losses that should be accepted as premium decay. |
| Tiered TPs on v2: +100%/+300%/trail | Captures certainty AND keeps the tail. Tail is the only thing that pays in a power-law distribution. |
| Multi-chain order: Sui before BNB; Base only after anti-rug infra | Base has 60% 48-hour rug rate per cited data; signing up without rug filters = adverse selection. Sui's Move-based contracts reduce certain exploit classes. BNB skipped indefinitely (no quality-venue evidence). |
| Universal graduation filter applies to ALL strategies | It's a quality gate, not a strategy variant. Verifiable on-chain fact (pair venue), not a heuristic. |
| Drop 2 sniper-tier wallets, keep 2 | Total cull would risk zero signals. 2-and-2 keeps cluster-detection capability while diversifying. |

---

## Work that remains (queued in task list)

### Immediate (next session, first 5 minutes)

1. Read tomorrow's 7am morning report (or check `tradebot.log` + `watchdog.log` directly if Cowork was closed overnight).
2. Confirm: did the watchdog catch any overnight crashes? Did the new wallet set produce any signals? Did the graduation filter reject any tokens?

### Short-term (next 1-2 working sessions)

3. **Phase 2 — Risk manager + main loop strategy-awareness.** Wire `strategy_tag` through:
   - `risk_manager.can_open_position(strategy_tag=...)` looks up the right config block via `get_strategy_config()`
   - `risk_manager.compute_position(strategy_tag=...)` returns sizing/SL/TP based on the strategy
   - `main.handle_signal()` passes strategy_tag from signal_data to risk_manager + open_position
   - `main.monitor_positions()` exit logic branches on strategy_tag: v1 uses single SL/TP, v2 uses tiered ladder + trailing stop on runner
   - Per-strategy `max_open_positions` caps so v2's 40 doesn't crowd out v1's 10 (or vice versa)
   - Estimated: 1-2 hours of careful work + testing

4. **Phase 3 — Structural filters.** Graduation already done (Phase 0.5). Remaining:
   - LP-lock check (read PinkLock / Streamflow / Bonk-lock program accounts via Helius for each LP)
   - Holder distribution: top-1 entity ≤3% (start with naive address check, layer bundle detection later)
   - Deployer wallet: hasn't moved their initial allocation
   - Estimated: 3-5 days

5. **Phase 4 — v2-options emitter + tiered exits.** Strategy emitter, tiered TP execution in monitor loop, trailing-stop logic for runner portion. Estimated: 4-5 days.

6. **Phase 5 — Measurement layer.** `candidate_observations` table + post-hoc tracker. Logs every candidate (accepted AND rejected) with full feature vector; re-polls at 1h/6h/24h/7d to build counterfactual dataset. Independent of trading logic so it can run without risk to baseline. Estimated: 2-3 days.

### Medium-term (after Phase 4 produces ≥30 v2 trades)

7. Split P&L by `strategy_tag` and decide: keep v2-options, kill v1-momentum-baseline, or tune.
8. Add CEX-listing tracker (`v2-cex-listing`) once Phase 4 emitter framework exists — the WIF-Upbit pattern (+44% / 300% volume on confirmed listings) is a discrete event-driven signal.

### Long-term (only after Phase 5 has 2-4 weeks of data)

9. Add Sui as second chain. Requires separate tooling (Move-based, no shared EVM infra). Smaller-cap ecosystem means lower liquidity floors needed.
10. Then Base — only with the anti-rug filters from Phase 3 actually proving themselves on Solana first.

---

## Files touched this session

- `config.py` — added `STRATEGIES` dict, `get_strategy_config()`, `REJECT_PRE_GRADUATION_DEX_IDS`, `ENFORCE_GRADUATION_FILTER`
- `database.py` — 5 new column migrations (strategy_tag × 3 tables + tp_tier_state + peak_price_usd + remaining_token_amount on open_positions); updated `create_signal()`, `open_position()`, `close_position()` to accept the new fields
- `risk_manager.py` — added universal graduation filter in `can_open_position()`
- `run_all.py` — replaced devnull redirect with crash.log; added top-level unhandled-exception catcher
- `CLAUDE.md` — corrected scheduled task names; added task-discovery one-liner and manual-launch fallback
- `watchdog.ps1` — NEW; 5-minute liveness check
- `dump_wallets.py` — NEW; one-off DB inspector
- `apply_wallet_changes.py` — NEW; idempotent wallet set tune-up
- `docs/handoffs/2026-05-21-options-portfolio-commitment-and-phase-1.md` — this file
- Memory: `tradebot_objective.md`, `tradebot_options_portfolio_rebuild.md`, `tradebot_base_rates.md` (NEW), `MEMORY.md` (updated index)

## Files NOT touched (worth knowing)

- `wallet_tracker.py` — still emits signals without a `strategy_tag` field. Will be modified in Phase 2.
- `zombie_tracker.py` — same.
- `find_wallets.py` / `find_zombies.py` — discovery scripts unchanged. The 6am `find_wallets.py` scheduled run produced this morning's candidates.csv that we used for the wallet swap.
- `main.py` — no changes. Phase 2 will wire `strategy_tag` through `handle_signal()` and `monitor_positions()`.
- `.env`, `tradebot.db` — Helius key intact; DB preserved (just additive migrations).

---

## Gotchas the next session needs to remember

- **`crash.log` accumulates normal startup banners alongside crashes.** Grep for `UNHANDLED CRASH` to find real crashes. The banner accumulation is by design — easier to scroll past banners than to have crashes invisible.
- **Scheduled task names are `Tradebot-bot-always-on` / `Tradebot-find-wallets-daily` / `Tradebot-watchdog`.** Not just "Tradebot". CLAUDE.md now has the discovery one-liner.
- **`apply_wallet_changes.py` was a one-off for this session.** Don't re-run it unless you want it to no-op (it's idempotent). It's not a reusable workflow — use `find_wallets.py` → `candidates.csv` → dashboard's Add Wallet UI for ongoing wallet curation.
- **`DeprecationWarning` on `datetime.utcnow()` in `apply_wallet_changes.py`** is Python 3.12+ deprecation. Cosmetic, will become an error in some future Python release. Trivial fix if you care.
- **OneDrive sync makes `tradebot.db` unreadable from the sandbox while the bot is writing.** Reads from Windows side (`python dump_wallets.py`) work fine. From sandbox, must copy to /tmp and use `file:?mode=ro` URI — and even that may fail mid-WAL. Don't expect to be able to query the DB live from the bash mount.
- **The bash mount lags behind file-tool writes by several seconds** (same gotcha as prior handoffs). The file-tool view is authoritative; if a `tail` shows truncation, wait or re-read via the file tool.
- **Watchdog won't help if Windows is fully suspended / hibernated.** The task scheduler doesn't fire when the OS isn't running. Watchdog only catches the "PC awake but bot dead" or "PC slept then woke without re-logon" cases.

---

## Quick health check for the next session

```powershell
# 1. Is the bot up?
Get-Process pythonw -ErrorAction SilentlyContinue
(Invoke-RestMethod http://localhost:3001/api/snapshot).stats

# 2. Watchdog activity overnight?
Get-Content C:\Users\hp\OneDrive\Documents\Claude\Projects\Tradebot\watchdog.log -Tail 30
# look for any "STARTED" or "STALE" lines among the "OK" lines

# 3. Any crashes?
Select-String "UNHANDLED CRASH" C:\Users\hp\OneDrive\Documents\Claude\Projects\Tradebot\crash.log

# 4. New signals / trades since session end?
cd C:\Users\hp\OneDrive\Documents\Claude\Projects\Tradebot
python -c "import sqlite3; c=sqlite3.connect('tradebot.db'); print('signals last 24h:', c.execute(\"SELECT COUNT(*), signal_type, strategy_tag FROM signals WHERE created_at >= datetime('now','-1 day') GROUP BY signal_type, strategy_tag\").fetchall()); print('closes last 24h:', c.execute(\"SELECT COUNT(*), SUM(pnl_usd) FROM trade_history WHERE closed_at >= datetime('now','-1 day')\").fetchone())"

# 5. Graduation filter activity
Select-String "Pre-graduation" C:\Users\hp\OneDrive\Documents\Claude\Projects\Tradebot\tradebot.log -SimpleMatch | Measure-Object | Select-Object Count
```

If all green → start Phase 2 (risk manager + main loop strategy-awareness). If anything red → triage that first, save the symptom to project memory if it's load-bearing context.
