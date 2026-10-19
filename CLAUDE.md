# Tradebot — Project Briefing for Claude

Read this first. It says what is running, where it lives, how changes ship, and what not to touch. A plain-language explanation of the flow, the scoring model and how edge is judged is in `docs/HOW_IT_WORKS.md`.

## What this is

A Solana token research service. It scans, scores and measures; it cannot trade. There is no wallet, `SOLANA_PRIVATE_KEY` is rejected at startup, and `RESEARCH_EXECUTION_ENABLED` must be `false`. Owner: Kaye (oebiefie@gmail.com).

There is **one bot**: the managed service in `tradebot/`, hosted on Render with Neon Postgres. The old Windows bot (`main.py`, `run_all.py`, SQLite `tradebot.db`) is retired; see "Legacy" at the end.

| Piece | Role |
|---|---|
| GitHub `GinniNio/Tradebot`, branch `main` | The code. Every change goes in by PR; CI must pass. |
| Render service `tradebot-managed-staging` | The only running instance: dashboard, API and background scanner. Deploys automatically from `main`. |
| Neon Postgres | All data. Replaced SQLite. |
| Dexscreener | The only market-data provider wired in. Helius, Jupiter, RugCheck and Telegram are configured as budgets only and are not called. |
| OneDrive | Exported CSV reports and backups only. Never a working repo or database. |

Live URL: **https://tradebot-managed-staging.onrender.com** (dashboard login = `OPERATOR_DASHBOARD_USERNAME` / `OPERATOR_DASHBOARD_PASSWORD` in Render). Kaye uses it from either laptop in a browser. Routine use needs no terminal or database credentials.

## Current status (2026-10-19)

- Deployed: `e0f46b3` (PRs #8 and #9), migration `0003_research_experiment`, strategy version `market_watch_v2`.
- Scanner active from the 06:11 UTC redeploy. **7-day collection checkpoint: about 2026-10-26**, once two scheduled cycles on this deploy are confirmed. That review is about throughput (qualifying tokens, rejection reasons, completed and unresolved checks), not profitability.
- Why v2 exists: in 30 days v1 scored 7,270 rows and found 1 eligible token and 5 score-only rejects, and outcomes were only measured after a manual dashboard click. A live sample on 2026-10-19 found the Dexscreener profile feed is almost all tokens under 10 hours old, and liquidity under $50k is the dominant rejection.
- Decision rule after the checkpoint: if v2 still produces almost no qualifying tokens, change the discovery source or retire the experiment. Do not loosen filters to make numbers look active.

## Operator routine

1. Open the URL. `/` redirects to `/report`.
2. Check the Status box: last successful scan, and anything flagged (no successful scan in 45 minutes, a failed scan, failures in 24h, checks overdue by more than 30 minutes, scanner not active).
3. Weekly: read `/report` (7 or 30 days) and download the summary or observations CSV if wanted.
4. `/candidates` shows the market-watch cards. Watch/Reject are optional notes; they do not start or affect experiment measurement.

## How the service works

Startup (`tradebot/app.py` lifespan):
1. The container runs `alembic upgrade head`, then uvicorn (`Dockerfile`). A failed migration stops the new instance; Render keeps the previous one.
2. `load_settings()` validates env (`tradebot/config.py`).
3. `Database.connect()` retries transient errors at 1/2/4/8 s (`tradebot/db.py`).
4. `AdvisoryLock` connects with `SCANNER_LOCK_DATABASE_URL` and takes a Postgres advisory lock, so only one scanner runs across deploy overlap. If another instance holds it (normal during a deploy) or the connection fails, the instance stays up in `standby` and retries every `SCANNER_LOCK_RETRY_SECONDS` (default 30), starting the scanner once it gets the lock.
5. `ManagedScanner` runs `MarketWatchService.run_cycle` every `SCANNER_INTERVAL_SECONDS` (default 900).

Each cycle (`tradebot/market_watch.py`) writes one `scan_cycles` row with counts and rejection reasons, then:
1. Processes due outcome checks: up to 60 claimed, prices batch-fetched 30 pairs per call, SOL/USD fetched for the benchmark.
2. Unless research is paused: discovers up to 30 tokens from `/token-profiles/latest/v1`, and rechecks up to 300 tokens first seen 1–7 days ago. Rechecks that fail the hard filters are counted, not stored.
3. Scores each pair with the frozen `swing_quality` rules (`tradebot/strategy.py`): five hard filters (liquidity ≥ $50k; 24h volume ≥ $100k; market cap or FDV $250k–$25M; 24h change +5% to +120%; 1h change ≤ +25%), then a 100-point score that must reach 65.

### Experiment rules (frozen in `tradebot/experiment.py`, stored in `strategy_versions.config_json`)

- **Eligible cohort**: a token's first observation with score ≥ 65, on any scan, starts +1h/+24h/+3d/+7d checks from that observation's price. Once per token per strategy version (idempotency key `exp:{version}:{cohort}:{token}:{kind}`).
- **Control cohort**: a fixed hash-selected half of tokens that pass all five hard filters but score < 65. Never rewritten if the token later qualifies.
- **Primary horizon**: 24h. Decided in advance; do not switch after seeing data.
- **Missing data**: `scheduled_at` is never changed; retries move only `due_at`. A check with no valid price inside its tolerance (1h: 15 min, 24h: 2 h, 3d: 6 h, 7d: 12 h) becomes `unresolved` with a reason (`pair_missing`, `invalid_price`, `provider_error`, `missed_window`). **Never store an invented price or an assumed -100%.** Reports show a separate conservative scenario that values unresolved checks at -100%.
- Claims stuck in `claimed` for 10+ minutes (crash or restart) are reclaimed.
- Changing any threshold, cohort rule, horizon or discovery behaviour means a new `STRATEGY_VERSION` and a fresh evaluation period. Never pool versions.

### Reporting (`tradebot/verdict.py`)

One set of calculations is used by `/report`, `/report/summary.csv`, `/report/observations.csv` and `python -m tradebot.verdict [--days N] [--json]` (needs only `DATABASE_URL`). Per cohort and horizon it shows tokens, observed, unresolved by reason, pending, mean, median, share losing, worst and best, mean without the top 1 and top 2, top-winner share of gains, the conservative scenario, SOL over the same windows and excess return, 2% and 5% cost sensitivity, chronological halves with n, and medians by discovery day. Returns are Dexscreener market-price changes, not executable fills.

Before calling anything promising: the median and the mean must both hold up, results must beat SOL and the control cohort, must not depend on one token or one day, and must survive costs. Then the next step is quote-only (Jupiter) entry and exit checks on a fresh batch. Still no wallet.

## File map (`tradebot/`)

| File | Role |
|---|---|
| `app.py` | FastAPI app: lifespan, `/healthz`, `/report`, CSV exports, `/candidates`, decision and pause APIs, HTTP Basic auth. |
| `config.py` | Env validation and provider budgets. |
| `db.py` | asyncpg pool with connect retry; `migration_status`. |
| `advisory_lock.py` | Scanner lock. Keepalives are applied with `SET` after connecting (see gotchas). |
| `scanner.py` | Background loop; records a redacted error category per cycle. |
| `market_watch.py` | Cycle: outcomes, discovery, recheck, persistence, experiment scheduling. |
| `strategy.py` | Frozen scoring; `STRATEGY_VERSION = "market_watch_v2"`. |
| `experiment.py` | Frozen experiment rules and control sampling. |
| `queue.py` | Outcome-check claim, retry, unresolved and complete SQL. |
| `repository.py` | Candidate listing and operator decisions. |
| `dexscreener.py` | Rate-budgeted client: discovery, pairs, batch pairs, SOL price. |
| `verdict.py` | Report calculations, CSV, CLI. |
| `health.py`, `doctor.py` | Health payload; one-off diagnostic CLI. |

Migrations live in `alembic/versions/` (0001 foundation, 0002 market watch queue, 0003 research experiment). They are additive. Tables: `strategy_versions`, `source_events`, `research_candidates`, `market_snapshots`, `candidate_decisions`, `outcome_checks`, `scan_cycles`, `operator_settings`, plus `quote_snapshots` and `evidence_items` (created, not yet written to).

## Shipping a change

1. Branch, change, then run locally: `ruff check tradebot tests --target-version py312` (CI runs Python 3.12; without the flag, newer ruff misses UP017), `python -m pytest -m "not postgres"`, and the Postgres tests when the change touches SQL (below).
2. Open a PR. CI jobs: `test` (lint plus unit tests) and `postgres-migrations` (upgrade twice, downgrade one step and back, `pytest -m postgres`).
3. Squash-merge when green. Render deploys `main` automatically, and the container applies migrations.
4. Verify on the live `/healthz`: `build.git_commit_sha` is the new commit, `database.migration.revision` is the expected head, `scanner.state` is `active`, and `cycles_completed` rises on schedule. A standby scanner is a failed deploy even if the page loads.

Postgres tests locally: start a disposable Postgres 16, `alembic upgrade head`, then `TRADEBOT_TEST_DATABASE_URL=postgresql://... python -m pytest -m postgres`. They truncate their tables.

## Environment (Render; template in `.env.managed.example`)

- `DATABASE_URL`: Neon application URL (pooled is fine).
- `SCANNER_LOCK_DATABASE_URL`: Neon **direct, non-pooled** URL (host without `-pooler`) with **current** credentials. Advisory locks need a session connection.
- `APP_ENV=production` requires `OPERATOR_DASHBOARD_USERNAME` and `OPERATOR_DASHBOARD_PASSWORD`.
- `GIT_COMMIT_SHA` or Render's `RENDER_GIT_COMMIT` must identify the deployed commit.
- `RESEARCH_EXECUTION_ENABLED=false`, `LAUNCH_TRACKER_ENABLED=false`, and four `<PROVIDER>_MAX_CONCURRENCY/_TIMEOUT_SECONDS/_MAX_RETRIES/_RATE_BUDGET_PER_MINUTE` sets (DEXSCREENER, HELIUS, JUPITER, RUGCHECK).
- Never set `SOLANA_PRIVATE_KEY`.

## Known gotchas

- **Lock credentials.** On 2026-10-19 the lock URL's credentials were rejected (`InvalidPasswordError`). Before #8 that crashed startup (uvicorn exit status 3, the morning alert) while an old, already-connected instance kept serving. After #8 it showed as `scanner.state: standby`. Fixed by updating `SCANNER_LOCK_DATABASE_URL` in Render. If the Neon password is rotated, update both URLs.
- **No startup parameters on the lock DSN.** asyncpg sends DSN query parameters to the server as startup parameters. Standard Postgres rejects libpq's `keepalives*` names. Keepalives are applied with `SET tcp_keepalives_*` after connecting, best effort.
- **Exit status 3** from uvicorn means the app's startup raised. The cause is in the log lines just before `Application startup failed`.
- **Dexscreener fields can be missing** (for example `priceChange.h1` on young pairs). `strategy._d` treats unparseable values as missing; keep it that way.
- **Storage.** `source_events` and `market_snapshots` keep raw JSON for every stored observation. Rechecks store only hard-filter passes to keep Neon small.
- `HEAD /` returns 405 in Render logs: Render's port probe. Harmless.

## Operating doctrine (locked)

1. Every signal is data before it is a trade. No strategy is tradable; the service has no execution path.
2. Measure from the first qualifying observation, count each token once, keep missing data visible.
3. Do not retune thresholds against the same data. A new rule needs a new version and fresh forward data.
4. No new providers, dashboards, alerts or strategies during the current evaluation.
5. **Kill criterion**: if no strategy shows repeatable, cost-aware edge by **2026-12-31**, archive or downgrade the project. Stop admitting new 7-day cohorts by 2026-12-24 if they must mature before that decision.

Legacy strategy dispositions (from `docs/SOLO_OPERATOR_SCOPE.md` and the May–June research): `zombie_revival` STOP (n=41, median 24h -77%); `zombie_revival_v2` legacy RESEARCH pending its forensic, with no new discovery; `launch_momentum` to be formally moved to STOP; `wallet_convergence` was paused. None of these run in the managed service.

## What NOT to do without asking

- Don't add a wallet, signing, or any execution path, and don't set `SOLANA_PRIVATE_KEY`.
- Don't change scoring thresholds or experiment rules in place. Bump the version, and only with Kaye's agreement.
- Don't write assumed prices or losses into `outcome_checks`.
- Don't drop or rewrite columns. Migrations are additive.
- Don't point Render at `main.py`, `server.py` or `run_all.py`.
- Don't commit `.env`, any `*.db`, or exported data. `.gitignore` covers SQLite and its `-wal`/`-shm` files.
- Don't run a second scanner against the same Neon database outside the advisory lock.

## Calendar

- 2026-05-22 to 2026-07: legacy Windows bot research (zombie verdict 2026-05-28, graduation tracker 2026-06-05).
- 2026-09-09/10: managed rebuild (PRs #2–#7): Render, Neon, market-watch queue, dashboard.
- 2026-10-19: v1 audit (1 eligible in 30 days). PR #8: `market_watch_v2` automatic measurement, recheck, `/report` and CSVs, migrations on start. PR #9 plus a Render env fix: the scanner lock works again on Neon.
- ~2026-10-26: 7-day collection checkpoint (throughput, not profitability).
- 2026-12-24: last day to admit 7-day cohorts that must mature before the decision.
- 2026-12-31: kill-criterion decision.

## Legacy (retired)

The flat files at the repo root (`main.py`, `run_all.py`, `server.py`, `config.py`, `database.py`, the `*_tracker.py` modules, `jupiter_client.py`, `helius_client.py`, `birdeye_client.py`, `static/`) are the May–July Windows bot. It is not running (no process, no scheduled task, as checked on 2026-10-19), and the managed service must not import it (`tests/test_app_imports.py`).

Its data is **backed up (2026-10-19 11:26 local)**: byte-identical raw copies plus a clean single-file `-snapshot.db` per database (integrity ok), the uncommitted code edits as a patch, and untracked files. Local: `C:\Dev\Tradebot-backup-20261019-1126`. OneDrive: `Tradebot-backups\tradebot-legacy-20261019-1126.zip` (111.3 MB, no `.env`). The originals in `C:\Dev\Tradebot\Tradebot` are no longer needed.

| Database | Ran until | signal_outcomes (live) |
|---|---|---|
| `tradebot.db` (main) | 2026-09-09 | launch_momentum 4,924; zombie_revival 290; zombie_revival_v2 56; swing_quality 4 |
| `tradebot-DESKTOP-2HRBHH4.db` (other PC) | 2026-07-19 | launch_momentum 577; zombie_revival 373; zombie_revival_v2 75 |

The two have **diverged**: the other PC's copy holds zombie outcomes that the main one lacks. Neither is complete. Merge them only if the zombie or launch research is ever reopened, and de-duplicate by token and time, because signal ids collide. Legacy outcomes are a comparison cohort only and are never mixed with managed-service results. `OPERATIONS_MANUAL.md`, `HOW_THE_BOT_WORKS.md` and `TRADEBOT_CHEAT_SHEET.pdf` describe the legacy bot.

Development on either laptop: keep a separate git clone outside OneDrive (for example `C:\Dev\Tradebot\Tradebot-rebuild`), pull before working, push via PR.
