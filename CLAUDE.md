# Tradebot — Project Briefing for Claude

This file orients a fresh Claude session. Read it first; it tells you what's running, where things live, and what to check before touching anything.

## What this is

A Solana memecoin paper-trading bot running 24/7. The current operating mode is **research-only**: signals fire and outcomes get logged, but no positions open. A strategy state machine in `risk_manager.can_open_position()` blocks every trade unless the strategy is explicitly promoted from RESEARCH to PAPER. There has been no LIVE trading. Owner: Kaye (oebiefie@gmail.com). Project root: `C:\Users\hp\OneDrive\Documents\Claude\Projects\Tradebot`.

Three signal sources exist:

1. **Wallet tracker.** Polls smart wallets via Helius. When `WALLET_TRIGGER_COUNT` distinct wallets buy the same token within `WALLET_LOOKBACK_MINUTES`, emits a wallet signal. **Currently disabled.** Helius free-tier quota exhausted 2026-05-22; resets 2026-06-20. Tracker gated off via `WALLET_TRACKER_ENABLED=False` in `.env`.
2. **Zombie tracker.** Watches dormant tokens (no volume for ≥14 days) for revival. Was the only live signal source until 2026-05-28 when the verdict query archived zombie_revival (see strategy table below). Tracker still runs and outcomes still log, but every signal hits `blocked_by_strategy_state:STOP`. Runs in Dexscreener-only mode (Birdeye disabled — Standard plan 1 RPS / 30k CU/mo can't sustain per-token dormancy checks).
3. **Graduation tracker.** Added 2026-06-05. Polls pump.fun's public API every 60s for tokens that just graduated from the bonding curve (complete=True). Enriches each graduate with Dexscreener stats, applies filters (liquidity ≥ $15k, market cap ≤ $500k, volume_1h ≥ $2k, age ≤ 15 min), and fires launch signals. Detection lag ~1-2 min. Strategy state is RESEARCH — outcomes log but no positions open. Kill switch: `LAUNCH_TRACKER_ENABLED=False` in `.env`.

Signals flow: tracker → `create_signal()` in DB → `signal_outcome_tracker.record_signal_outcome()` (inserts outcome row + enqueues 4 pending price checks at +5m/15m/1h/24h) → `main.handle_signal()` → `risk_manager.can_open_position()` (state gate = FIRST check, blocks all RESEARCH strategies) → would call `jupiter_client.buy_token()` if approved (currently never reached).

A background loop (`signal_outcome_tracker.process_pending_checks_loop`) wakes every 60s and fulfills due price checks via batched Dexscreener calls. This is DB-backed, so it survives restarts.

A FastAPI server on port 3001 exposes a research-mode dashboard. The frontend was rewritten 2026-05-22 to lead with measurement panels (Research Health, Signal Outcomes, Pending Checks) rather than the old trading view.

## Operating doctrine (locked)

1. Every signal is data before it is a trade.
2. RESEARCH signals log outcomes but cannot open positions.
3. Route arbitrage is RESEARCH until quote gaps prove persistence (>50bp spreads surviving >500ms).
4. Zombie revival was RESEARCH until 2026-05-28 verdict (n=41, median -77%, archived to STOP). Any future revival-style strategy needs a redesigned selector and a new strategy key.
5. v2 is an execution profile, not evidence of edge.
6. Wallet convergence requires independent wallets, not clustered bots.
7. Creator reputation is passive collection until enough data exists (60-90 days).
8. Tiered candidate funnel: Dexscreener → Jupiter → GoPlus → Helius. No source enters a wide polling loop.
9. The first analytics table is `signal_outcomes`; keep it small until proven worth extending.
10. **Kill criterion**: if no strategy reaches LIVE_CANDIDATE by 2026-12-31, archive or downgrade the project.

## Strategy state machine

Every signal source has an explicit state in `config.STRATEGY_STATES`. `risk_manager.can_open_position()` calls `strategy_can_open_position()` as the first gate, before liquidity/balance/graduation checks.

| Strategy | State | Why |
|---|---|---|
| zombie_revival | **STOP** | Archived 2026-05-28. n=41 live outcomes, median 24h -77%, mean -62%, 12% positive. Selector picks tokens that mostly die. See "Zombie verdict" below. |
| zombie_revival_v2 | RESEARCH | Registered 2026-05-28. Tightened selector based on v1 winners-vs-losers forensic: pc_1h in [20%, 50%], vol_1h ≤ $8k, chain in {base, solana}. In-sample filter — needs ≥30 fresh outcomes before any promotion call. |
| wallet_convergence | PAUSED | Helius credits exhausted until 2026-06-20; sniper-bot-contaminated universe |
| launch_momentum | RESEARCH | Built 2026-06-05. `graduation_tracker.py` polls pump.fun for graduates, enriches via Dexscreener. ~1-2min detection lag (Helius websocket will close this post-2026-06-20). Needs >=30 outcomes before verdict. |
| route_arbitrage | RESEARCH | Persistence test must precede paper trading |
| ai_agent_attention | BACKLOG | Idea only, no code path |

Tradable states: PAPER, LIVE_CANDIDATE, LIVE. Promotion gates: 30-50 logged outcomes for RESEARCH→PAPER, 100 paper trades for PAPER→LIVE_CANDIDATE, 250+ trades + manual review + kill switch for LIVE_CANDIDATE→LIVE. STOP is terminal — strategy proven negative-EV, do not re-litigate without a fresh selector and a new strategy key.

### Zombie verdict (2026-05-28)

Verdict-query result on n=41 live outcomes with price_24h populated:

- mean 24h return: -62.6%
- median 24h return: -77.3% (worse than mean = systemic decay, not tail rugs)
- 12% positive at 24h
- worst 5: -100, -97, -96, -95, -95
- best 5: +4, +5, +8, +37, +63
- route_rate: 100% (Jupiter quotes always available — execution wasn't the constraint)

Early-window shape: avg_5m +5.3%, avg_15m +13.9%, avg_1h -0.3%, avg_24h -62.6%. Pop in the first 15min, gone by 1h, dead by 24h.

Why a scalp variant (15m time-exit) was rejected:
- The 13.9% avg at 15m is averaged across signals — for the 88% of tokens that ultimately die, that early print is dead-cat momentum, not capturable edge once you price in slippage on a collapsing book.
- At signal time there's no feature distinguishing the 12% real revivals from the 88% dying bounces. The candidate selector itself is wrong.

Action: zombie_revival flipped to STOP in config.STRATEGY_STATES. Background signal_outcomes accumulation continues for any in-flight pending price checks but no new selector tuning. Forensic on the 5 winners vs the 36 losers (2026-05-28) surfaced three separators — see "Zombie v2 hypothesis" below — and a new strategy key `zombie_revival_v2` was registered as RESEARCH.

### Zombie v2 hypothesis (2026-05-28)

Forensic finding: winners are quiet, losers are loud. Pulled from the same 41 outcomes that drove the v1 verdict.

| feature | winners (n=5) | losers (n=36) | catastrophes (n=17) |
|---|---|---|---|
| volume_1h median | $3,385 | $10,774 | $12,568 |
| price_change_1h median | +20.8% | +32.5% | +25.7% |
| price_change_1h max | +49.2% | **+1,250%** | +63.8% |
| 15m_return median | ~0% | -5% | -14% |
| Base chain catastrophes | 0/2 | 0/3 | 0/0 |
| Solana chain catastrophes | n/a | 16/24 | 16/16 |

v2 selector applied at signal-fire time inside `zombie_tracker._fire_zombie_signal`:
1. `price_change_1h_pct` ∈ [20%, 50%] — rejects FOMO blow-offs (config.ZOMBIE_V2_PC1H_MIN/MAX)
2. `volume_1h` ≤ $8,000 — prefers quiet revivals (config.ZOMBIE_V2_VOL1H_MAX_USD)
3. `chain` ∈ {`base`, `solana`} — Base had 0 catastrophes (config.ZOMBIE_V2_ALLOWED_CHAINS)

Signal routing: `signals.signal_type` stays `zombie` (CHECK constraint unchanged). When v2 filters pass, `record_signal_outcome` is called with `signal_type='zombie_v2'`, which maps via SIGNAL_TYPE_TO_STRATEGY to `zombie_revival_v2` (RESEARCH) instead of `zombie_revival` (STOP). The two cohorts measure separately in `signal_outcomes.strategy`.

In-sample warning: the v2 selector was derived from the same 41 outcomes it's being evaluated against. Treat the first ≥30 fresh outcomes as a true out-of-sample test. Do not retune mid-flight.

`find_zombies.py` mirrors the v2 filter at discovery time and adds Base chain support (was Solana-only). Tunables there must stay aligned with `config.ZOMBIE_V2_*` — if one changes, change both.

### Watchlist `dormant_since` bug fix (2026-05-28)

Pre-2026-05-28, every row in `zombie_watchlist.dormant_since` was NULL because all callers of `add_to_zombie_watchlist` defaulted `last_active_at=None`. Fix: `_refresh_watchlist`, `add_token`, and `seed_from_dexscreener` now pass approximate ISO timestamps (Birdeye path: now - ZOMBIE_DORMANT_DAYS; Dexscreener path: now - 1 day). The signal_outcomes forensic feature `dormancy_days` should populate on go-forward rows. Old rows stay NULL; `_fire_zombie_signal` falls back to `added_at` when `dormant_since` is missing.

`SIGNAL_TYPE_TO_STRATEGY` in config maps the tracker's `signal_type` ('zombie', 'wallet', etc.) to the strategy state key. Unknown types fail closed (default to RESEARCH).

## What's running 24/7

- **Windows Scheduled Task `Tradebot-bot-always-on`**: triggers At Logon, runs `pythonw.exe run_all.py` hidden. Auto-restarts on crash.
- **Windows Scheduled Task `Tradebot-find-wallets-daily`**: 6 AM daily, refreshes the smart-wallet candidate pool. Currently moot — wallet tracker is disabled.
- **Cowork scheduled task `tradebot-morning-report`**: daily health summary, includes calendar milestone tracking (first live signal, 30-outcome verdict gate, post-June-20 wallet-enable nag, etc.).
- **Cowork scheduled task `tradebot-reenable-wallet-tracker`**: one-shot, fires 2026-06-20 08:00 local. Edits `.env` to flip `WALLET_TRACKER_ENABLED=True` and prompts Kaye to restart.
- **Dashboard**: http://localhost:3001 (when bot is up). Default tab is Research Health.

To verify the bot is alive:
```powershell
(Invoke-RestMethod http://localhost:3001/api/ops_health) | Format-List
Get-Content C:\Users\hp\OneDrive\Documents\Claude\Projects\Tradebot\tradebot.log -Tail 30
```

If either Windows task ever fails to resolve, list what's actually registered:
```powershell
Get-ScheduledTask -TaskName "*Tradebot*" | Select TaskName, TaskPath, State
```

## Restart procedure

```powershell
Get-Process pythonw -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep 3
Start-ScheduledTask -TaskName "Tradebot-bot-always-on"
```

Fallback if the scheduled task is broken or missing — run the launcher directly:
```powershell
cd C:\Users\hp\OneDrive\Documents\Claude\Projects\Tradebot
Start-Process pythonw.exe -ArgumentList "run_all.py" -WindowStyle Hidden
```

Or reboot — the task fires on logon.

## File map

| File | Role |
|---|---|
| `main.py` | Tradebot orchestrator. Boots zombie tracker, monitor_positions, dashboard loop, and signal_outcome_tracker background loop. Wallet tracker conditionally started based on `WALLET_TRACKER_ENABLED`. |
| `run_all.py` | Launcher — runs `main.py` + uvicorn dashboard together. Entry point for the Windows scheduled task. |
| `config.py` | All tuneables. `STRATEGY_STATES` + `TRADABLE_STATES` + `strategy_can_open_position()` helpers live here. API budget doctrine at top of file. `.env` loaded via dotenv. `_strip_placeholder()` treats `your_xxx_here` as unset. |
| `database.py` | SQLite schema + queries. `_apply_migrations()` is additive-only. New helpers for signal_outcomes / pending_price_checks: `insert_signal_outcome`, `schedule_price_check`, `get_due_price_checks`, `complete_price_check`, `fail_price_check`, `signal_outcomes_summary`, `get_signal_outcomes_list`, `get_pending_price_checks_list`, `get_ops_health`. |
| `server.py` | FastAPI dashboard + WebSocket. Snapshot includes `strategy_states` and `strategy_health`. New endpoints: `/api/ops_health`, `/api/strategy_health`, `/api/signal_outcomes?source=`, `/api/pending_price_checks`. |
| `static/index.html` | Research-mode dashboard. RESEARCH banner up top, state badges in sidebar, Research Health / Signal Outcomes / Pending Checks tabs as defaults. Create-Strategy button hidden. |
| `wallet_tracker.py` | Polls smart wallets via Helius. Conditionally started by main.py. |
| `zombie_tracker.py` | Polls dormant tokens via batched Dexscreener (`get_token_stats_batch`). `_fire_zombie_signal` calls `record_signal_outcome` and runs a single Jupiter quote for route-availability capture. `ZOMBIE_USE_BIRDEYE=False` keeps Birdeye out of the loop. |
| `risk_manager.py` | Pre-trade gates + position sizing + exit decisions. **State gate is the FIRST check** in `can_open_position()`. Decision-only, no DB writes. |
| `jupiter_client.py` | Jupiter v1 swap API wrapper. Lazy-imports solders/solana; paper mode short-circuits. Note: Jupiter Swap V2 has superseded V1; migration is on the roadmap but not urgent for paper. |
| `helius_client.py` | Helius Enhanced Transactions API. `parse_swap_for_tracker()` normalizes swaps. Currently idle. |
| `birdeye_client.py` | Birdeye Data API wrapper. Demoted to enrichment-only use; not in any polling loop. |
| `dexscreener_client.py` | Primary cheap market-data source. **`get_token_stats_batch(addresses, preferred_chain)`** batches up to 30 tokens per call — use this in any new polling code. `revival_check_from_stats()` is the pure-logic revival rule. |
| `signal_outcome_tracker.py` | Measurement layer. `record_signal_outcome(signal_id, signal_type, token_address, chain, price_at_signal, route_available, token_symbol)` inserts the outcome row + enqueues 4 checks, then pushes a Telegram alert via `telegram_notifier.notify_signal` (no-op if unconfigured). `process_pending_checks_loop()` is the background coroutine main.py runs. `check_route_available_solana()` does the per-signal Jupiter probe. |
| `telegram_notifier.py` | Push alerts to Kaye's phone (added 2026-06-11). `send_message` (async), `send_message_nowait` (fire-and-forget from sync code in a running loop), `notify_signal` (called from the record_signal_outcome choke point), `notify_error` (throttled, one alert per key per 6h). Fail-silent by design: unconfigured = no-op, network errors = WARNING log + False. CLI: `python telegram_notifier.py "test"` and `python telegram_notifier.py --get-chat-id`. Config via `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` in `.env`. |
| `backfill_signal_outcomes.py` | One-shot CLI. Creates `source='backfill'` rows from historical `signals`. Idempotent. Does NOT enqueue pending checks. Does NOT fill price_24h with current prices. Run with `--apply` to write. |
| `find_wallets.py` | Discovery script — scans Dexscreener trending → Helius buyers → scores candidates. |
| `prune_wallets.py` | Reads tradebot.db, scores tracked wallets DROP/KEEP/WATCH. `--apply` deletes. |
| `find_zombies.py` | Discovery script — finds dormant tokens showing v2-shaped revival signs across `CHAINS` (Solana + Base as of 2026-05-28). Writes `zombies.csv`. Filter constants must stay aligned with `config.ZOMBIE_V2_*`. |
| `graduation_tracker.py` | Polls pump.fun for recently graduated tokens (bonding curve complete). Applies entry filters (liquidity, market cap, volume, age). Emits `signal_type='launch'` signals into `signal_outcomes` via the same infrastructure as zombie signals. Kill switch: `LAUNCH_TRACKER_ENABLED` env var. |

## Current config snapshot

See `config.py` for the source of truth. Key values:

- `PAPER_TRADING = True` — never been live; don't flip without explicit user instruction
- `STRATEGY_STATES` — see Strategy state machine section above
- `WALLET_TRACKER_ENABLED` — env var, currently `False`. Will be flipped True on 2026-06-20 by the scheduled reminder.
- `ZOMBIE_USE_BIRDEYE` — env var, currently `False`. Do not flip without verifying Birdeye plan has spare RPS/CU budget.
- `WALLET_TRIGGER_COUNT = 6` — moot while tracker is off
- `MAX_OPEN_POSITIONS = 10` — moot while all strategies are RESEARCH; effective cap is 0
- `TRADE_SIZE_SOL = 0.1`
- `STOP_LOSS_PCT = 15.0` / `TAKE_PROFIT_PCT = 50.0` (v1-momentum-baseline; v2-options has tiered exits with no stop)
- `ZOMBIE_MIN_LIQUIDITY_USD = 15_000` — must match `risk_manager.py` zombie floor and `find_zombies.py` `MIN_LIQUIDITY_USD`. If you change one, change all three.

API keys live in `.env`:
- `HELIUS_API_KEY` — present but quota-exhausted until 2026-06-20
- `BIRDEYE_API_KEY` — present (Kaye's free-tier Standard key); enrichment-only use, not for polling
- `SOLANA_PRIVATE_KEY` — never needed (PAPER mode); leave empty until live trading is on the table
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — optional push alerts (signal fires + throttled tracker-failure alerts). Placeholders = disabled, all sends silently no-op. Setup steps in `telegram_notifier.py` docstring.

API budget doctrine (also documented in `config.py` top-of-file docstring): Dexscreener for broad cheap polling (batched), Jupiter for tradability checks only, GoPlus for reject-gate after Jupiter passes, Helius for expensive enrichment only after cheap filters shortlist a candidate. No source enters a wide polling loop without a per-cycle cost calculation.

## Known gotchas

- **Sniper-bot correlation.** Tracked wallets often move in lockstep. Trigger=6 mutes most of this. Real fix is to rebuild the wallet set with cluster scoring in `find_wallets.py` (cross_token ≤ 3, bot_score penalty). Defer until after Helius reset.
- **Malformed wallet addresses.** Pre-2026-05-22 logs show `Helius error 400: invalid address` for several short-form wallet entries in `smart_wallets`. Audit and prune BEFORE re-enabling wallet tracker on June 20. Query: `SELECT address, chain, length(address) FROM smart_wallets ORDER BY length(address)`. Solana addresses should be 32-44 base58 chars.
- **Jupiter "no routes" on small caps.** Some zombie tokens have liquidity but no Jupiter aggregator route — quote fails, paper trade would be skipped if PAPER were tradable. Currently irrelevant (state gate blocks first).
- **Liquidity floor alignment.** `config.ZOMBIE_MIN_LIQUIDITY_USD`, `risk_manager.py` `min_liq`, and `find_zombies.py` `MIN_LIQUIDITY_USD` — if any drift, signals will fire but get rejected at trade time.
- **`pythonw.exe` has no stdout/stderr.** `run_all.py` redirects `sys.stdout`/`sys.stderr` to `os.devnull` at startup. Don't remove that — the scheduled task will silently crash if `print()` runs against a None stream.
- **Windows console can't render box-drawing chars.** Log format uses `--- SIGNAL ---` ASCII, not Unicode dashes. UTF-8 reconfigure in `run_all.py` covers the file handler. Em-dashes in log strings can also crash cp1252 — don't add them.
- **DB migrations are additive only.** `_apply_migrations()` catches "duplicate column" and proceeds. Never drop columns.
- **OneDrive sync.** Project lives under OneDrive. The bash sandbox sometimes sees stale/truncated copies of files just after a save — that's a sandbox view problem, not a real file problem. Real files on disk are correct.
- **Birdeye Standard tier is too small for polling.** 1 RPS, 30k CU/mo. Per-token zombie checks blow the budget in seconds. Keep `ZOMBIE_USE_BIRDEYE=False` unless plan is upgraded.

## Database

SQLite at `tradebot.db` in project root. Tables:

- `smart_wallets` — tracked wallets (audit pending; see Known gotchas)
- `wallet_activity` — recent buy/sell events (used by trigger detection)
- `signals` — emitted signals with `extra_data` JSON
- `open_positions` — currently held tokens (should stay empty until a strategy is promoted to PAPER)
- `trade_history` — closed trades (legacy data only: 14 closed trades, all pre-state-machine)
- `zombie_watchlist` — tokens being watched for revival
- `signal_outcomes` — Phase 3. Every fired signal gets one row. `source='live'` for real-time, `source='backfill'` for historical (6 rows from the one-shot backfill). Has price_at_signal + price_5m/15m/1h/24h + route_available_at_signal. `UNIQUE(signal_id, source)`.
- `pending_price_checks` — Phase 3. DB-backed scheduler for post-signal price snapshots. Survives bot restarts. Background worker drains by `due_at <= now()`.

Inspect from PowerShell (no sqlite3 CLI on Windows by default — use a Python here-string):

```powershell
@'
import sqlite3
c = sqlite3.connect(r"C:\Users\hp\OneDrive\Documents\Claude\Projects\Tradebot\tradebot.db")
for r in c.execute("SELECT id, token_symbol, pnl_pct, exit_reason FROM trade_history ORDER BY closed_at DESC LIMIT 20"):
    print(r)
'@ | python -
```

The verdict query for promotion decisions (run when `live_outcomes >= 30`):

```sql
SELECT strategy, COUNT(*) AS signals,
       AVG(CAST(route_available_at_signal AS REAL)) AS route_rate,
       AVG(CASE WHEN price_5m  IS NOT NULL AND price_at_signal > 0 THEN price_5m  / price_at_signal - 1 END) AS avg_5m,
       AVG(CASE WHEN price_15m IS NOT NULL AND price_at_signal > 0 THEN price_15m / price_at_signal - 1 END) AS avg_15m,
       AVG(CASE WHEN price_1h  IS NOT NULL AND price_at_signal > 0 THEN price_1h  / price_at_signal - 1 END) AS avg_1h,
       AVG(CASE WHEN price_24h IS NOT NULL AND price_at_signal > 0 THEN price_24h / price_at_signal - 1 END) AS avg_24h
FROM signal_outcomes
WHERE source = 'live' AND price_at_signal IS NOT NULL
GROUP BY strategy;
```

## API endpoints

All read-only. Served by `server.py` on port 3001.

| Endpoint | Purpose |
|---|---|
| `GET /api/snapshot` | Full dashboard data + `strategy_states` + `strategy_health` |
| `GET /api/ops_health` | Compact counts: mode, tradable_strategies, open_positions, live_outcomes, backfill_outcomes, pending_checks, overdue_checks, wallet_tracker_enabled, zombie_use_birdeye, last_live_outcome_at, last_outcome_completed_at |
| `GET /api/strategy_health?source=live` | Per-strategy signal count, route rate, avg post-signal returns |
| `GET /api/signal_outcomes?source=live\|backfill\|all&limit=N` | Recent outcome rows |
| `GET /api/pending_price_checks?limit=N&overdue_only=true` | Pending checks ordered by due_at |
| `GET /api/positions`, `/api/signals`, `/api/wallets`, `/api/zombies`, `/api/stats` | Legacy read endpoints |
| `POST /api/wallets/add`, `/api/zombies/add`, `/api/strategy/create` | Legacy mutations; not used in research mode |

## Daily ops checklist

Most of this is automated. The morning report (`tradebot-morning-report` scheduled task) runs daily and produces a one-liner if everything's healthy, otherwise leads with the problem.

Manual checks (mostly optional now):

1. `Get-Content tradebot.log -Tail 30 | Select-String "ERROR|Traceback"` — any new errors?
2. Open `http://localhost:3001` → Research Health tab — confirm `Tradable strategies = 0`, `Open positions = 0`, `Overdue checks = 0`.
3. Weekly: review `signal_outcomes` row count growth. ~3 zombie signals/day means ~20 outcomes/week.
4. After config changes: restart the bot.

## Calendar (current cycle)

- 2026-05-22: Research-mode architecture shipped. All five builds locked (state machine, Dex batching, outcome tracker, backfill, dashboard).
- 2026-05-28: Verdict gate hit at 46 live signals (41 with price_24h populated). Result: zombie_revival archived to STOP. Median 24h -77%, mean -62%, 12% positive. See CLAUDE.md "Zombie verdict" section. zombie_revival_v2 registered as RESEARCH with tightened selector.
- 2026-06-05: `graduation_tracker.py` built and wired into main.py. Polls pump.fun for bonding-curve graduates, fires `launch_momentum` RESEARCH signals. DB migrated: signals.signal_type CHECK now includes 'launch'. Detection lag ~1-2 min; Helius websocket upgrade deferred until post-2026-06-20.
- Next: accumulate ≥30 launch_momentum outcomes for first verdict. Watch for pump.fun API shape changes (unofficial endpoint — monitor logs for repeated HTTP errors).
- 2026-06-11: Morning report found the graduation tracker had fired ZERO launch signals since it shipped (06-05) — pump.fun fetches failing silently (errors stringified to empty, non-200s logged at debug). Built `telegram_notifier.py` (push alerts for signal fires + tracker failures, wired into the record_signal_outcome choke point), fixed graduation_tracker error logging (%r not %s, last-problem capture), and added an empty-fetch streak alarm (WARNING + Telegram at 1h of consecutive empty fetches, re-alert daily). Root cause of the pump.fun failures NOT yet diagnosed — first streak alert after restart will carry the actual HTTP status/exception. Kaye still needs to do Telegram setup (BotFather token + chat id into `.env`) and restart the bot.
- 2026-06-20 08:00 local: `tradebot-reenable-wallet-tracker` scheduled task fires. Audit `smart_wallets` for malformed addresses, then flip `WALLET_TRACKER_ENABLED=True` and restart.
- 2026-12-31: Kill criterion deadline. If no strategy reached LIVE_CANDIDATE by then, archive or downgrade.

## Other reference docs

- `OPERATIONS_MANUAL.md` — long-form operator's guide (may be outdated; treat as historical).
- `TRADEBOT_CHEAT_SHEET.pdf` — one-page printable cheat sheet (may be outdated).

## What to NOT do without asking

- Don't flip `PAPER_TRADING = False` without explicit user instruction.
- Don't promote a strategy past RESEARCH without first running the verdict query and reviewing the result with the user.
- Don't add code to a wide polling loop that calls Helius, Birdeye, or GoPlus without checking the API budget doctrine in `config.py`.
- Don't delete `tradebot.db` — there's no migration story, and signal_outcomes history is lost forever.
- Don't drop columns. Migrations are additive only.
- Don't push to a remote — there isn't one set up, and the `.env` contains live API keys.
- Don't add em-dashes or fancy Unicode to log strings — Windows cp1252 will crash on them.
- Don't build new strategy logic or new data sources before the existing zombie strategy's edge has been measured. The doctrine is "stop building" until the verdict query produces a decision.
- Don't expand to new chains (Sui, Monad, Base execution) before any strategy has proven edge on Solana.

The four valid interventions before June 20:
1. Restart the bot if logging stops.
2. Fix outcome tracking if a live signal fires but no `signal_outcomes` row appears.
3. Investigate if `pending_price_checks` become overdue.
4. Review watchlist scaling if it grows past ~300 tokens.

Anything else is premature.
