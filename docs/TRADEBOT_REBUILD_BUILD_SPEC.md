# Tradebot Rebuild: Developer Build Specification

## Mission

Build Tradebot as a solo-operated Solana opportunity-research system. It must scan, scrape, analyse, research and rank candidates, then present a short human-review shortlist. The first release is strictly research-only: it records shadow picks and outcome data but cannot sign, broadcast or manage a real-money trade.

The governing product definition is [Solo Operator Scope](SOLO_OPERATOR_SCOPE.md). This document defines how to build it.

## Non-negotiable constraints

- One GitHub repository, one Neon Postgres database and one single-instance Render web service.
- The Render service hosts the FastAPI API/dashboard and its asynchronous research loop.
- A Postgres session-level advisory lock ensures only one scanner is active during a restart or Render rolling deployment.
- Every source event, quote, evidence item, decision and scheduled outcome check is durable in Neon.
- No Redis, Kafka, MongoDB, VPS fleet, self-hosted Solana node, Jito/Shredstream or smart contract.
- No automatic copy trading, launch sniping, MEV, direct pool swaps or real-money execution.
- Do not write, log or expose any private key. The research build must fail closed if live execution is enabled.
- Provider calls are bounded by configured concurrency, timeout, retry and rate-budget values.

## Starting state

The existing code is a local FastAPI + SQLite application. Reuse only its useful boundaries:

| Existing area | Rebuild treatment |
|---|---|
| `dexscreener_client.py` | Reuse as a bounded discovery/market-data client after adding explicit client budgets. |
| `helius_client.py` | Reuse for targeted candidate enrichment only. Do not use it for recursive creator-history scans. |
| `jupiter_client.py` | Replace its execution role with read-only quote helpers using current Jupiter Swap V2. No signing methods in the research path. |
| `signal_outcome_tracker.py` | Replace in-memory scheduling assumptions with Neon-backed due work. |
| `database.py` | Replace with a Postgres repository layer. Do not carry SQLite `?` placeholder SQL into production. |
| `server.py` and dashboard | Keep the dashboard/API shell, add health and candidate-card endpoints. |
| `launch_momentum` | Legacy strategy. Stop its tracker and Telegram alerts. Archive its state in the dedicated verdict/state PR. |
| `zombie_revival` | Legacy control data only. Do not create a new dormant-token scan in the rebuilt queue. |

Do not migrate old local alerts into the new candidate queue. Existing SQLite data is exported as a read-only legacy cohort, with its original source and timestamps where available.

## Deliverables

### Work package 0: freeze the legacy runner

1. Add `LAUNCH_TRACKER_ENABLED=False` to the production example/config default for the new deployment.
2. Add a delivery note explaining how the operator backs up the old SQLite database before cutover.
3. Keep legacy-state changes in their own reviewed PR:
   - `launch_momentum` moves to `STOP` after the authoritative verdict export is attached.
   - `zombie_revival_v2` stays `RESEARCH` until its forensic report results in `STOP` or a new strategy key.
4. Remove live execution setup instructions from the new deployment runbook. Do not remove historical code in this work package.

### Work package 1: managed foundation

Add:

- `Dockerfile` for the FastAPI service;
- `render.yaml` for one Render web service;
- a Neon `DATABASE_URL` configuration path for normal application queries;\n- a separate direct, non-pooled `SCANNER_LOCK_DATABASE_URL` for the advisory-lock connection;
- explicit environment validation;
- `GET /healthz` returning database, scanner, provider and build status;
- `doctor` command that validates database migration state, required research-provider configuration, configured budgets, health and alert configuration;
- GitHub Actions for formatting/linting, unit tests and migration tests;
- a deployment runbook that uses Render and Neon only.

Use a single Render instance. The background loop starts inside application lifespan only after it obtains the advisory lock:

```sql
SELECT pg_try_advisory_lock(<stable-bigint-lock-key>);
```

Hold the lock on the scanner's dedicated, non-pooled database connection. Configure that connection with TCP keepalives: `keepalives=1`, `keepalives_idle=15`, `keepalives_interval=5`, `keepalives_count=3`. If it cannot be acquired, keep the API alive but report scanner state `standby`; do not scan. Release it when that connection closes. A timestamp or table-based lease is not acceptable.

### Work package 2: Neon schema and repository layer

Use UTC `timestamptz`, UUID primary keys and Postgres numeric values for calculated amounts/prices. Use `numeric(38,0)` for token and SOL base-unit quantities and serialise those values as strings in API responses. Use `numeric(50,30)` for USD price values and percentage/ratio calculations. Do not use floating-point values for persisted base-unit amounts.

Create migrations for at least:

```text
strategy_versions
  id, strategy_key, version, git_commit_sha, config_json, created_at

source_events
  id, provider, provider_event_id, raw_payload_json, event_at, fetched_at,
  token_address, pair_address, chain, created_at

research_candidates
  id, token_address, chain, selected_pair, strategy_version_id,
  state, created_at, expires_at

market_snapshots
  id, candidate_id, liquidity_usd, volume_1h_usd, volume_24h_usd,
  price_usd, price_change_1h_pct, price_change_24h_pct,
  tx_count_1h, captured_at, raw_payload_json

quote_snapshots
  id, candidate_id, side, input_mint, output_mint, input_amount_base_units,
  output_amount_base_units, price_impact_pct, route_json, status,
  failure_reason, git_commit_sha, captured_at

evidence_items
  id, candidate_id, category, claim, source_url, source_transaction,
  publisher, source_at, fetched_at, classification, raw_payload_json

candidate_decisions
  id, candidate_id, strategy_version_id, git_commit_sha, rank,
  hard_rejects_json, reasons_json, operator_action, operator_note, decided_at

outcome_checks
  id, candidate_id, check_kind, due_at, status, completed_at,
  value_input_base_units, value_output_base_units, quote_snapshot_id, error
```

Requirements:

- `source_events.provider + provider_event_id` is unique when a provider ID exists.
- Outcome work is queried by `status='pending' AND due_at <= now()`.
- All creation APIs are idempotent.
- Store raw response JSON before derived fields.
- Return calculations require valid non-zero quote input/output. Dust handling uses fixed research notional and base-unit validity, not a universal dollar-price floor.
- Every decision and quote contains the exact Git commit SHA and strategy-version reference.

### Work package 3: provider clients and call budgets

Add a single provider-budget configuration block. Each provider client must use its own semaphore, timeout, bounded retry and exponential backoff on HTTP 429/transient errors.

Required environment names:

```text
DATABASE_URL
APP_ENV
RESEARCH_EXECUTION_ENABLED=false
DEXSCREENER_MAX_CONCURRENCY
DEXSCREENER_TIMEOUT_SECONDS
HELIUS_MAX_CONCURRENCY
HELIUS_TIMEOUT_SECONDS
JUPITER_MAX_CONCURRENCY
JUPITER_TIMEOUT_SECONDS
RUGCHECK_MAX_CONCURRENCY
RUGCHECK_TIMEOUT_SECONDS
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

Values belong in Render environment configuration, not source code. Validate that concurrency values are positive integers and that all timeouts are finite positive values.

Provider sequence for every new candidate:

1. Dexscreener discovers and supplies initial market state.
2. RugCheck performs the first safety/holder/liquidity/insider assessment.
3. Helius runs one bounded metadata/enrichment request only for candidates that pass step 2.
4. Jupiter is called only after safety checks pass.

A provider failure creates an explicit `unavailable` evidence/result state. It does not silently pass the candidate. RugCheck has its own circuit breaker: after the configured consecutive server-error threshold, pause candidate progression, emit one health alert and use a timed half-open probe before resuming.

### Work package 4: watch queue and safety pipeline

Implement the post-launch, sustained-liquidity watch queue.

1. Deduplicate incoming token/pair events.
2. Save the raw source event and market snapshot.
3. Apply `swing_quality` baseline filters as configurable research thresholds.
4. Run RugCheck and bounded Helius enrichment.
5. Record every hard reject and its reason.
6. For eligible candidates, create bidirectional Jupiter quotes at each research size.

Bidirectional quote rule:

1. Request a SOL-to-token buy quote at the configured research notional.
2. Read the token output in exact base units from that quote.
3. Use that exact output amount as the token-to-SOL sell-quote input.
4. Persist both quote responses and calculate the round-trip result from base units.
5. Reject when the sell quote fails, has zero output, breaches the configured price-impact cap or no longer represents the selected pair/context.

Never fabricate an exit price from Dexscreener price alone.

### Work package 5: evidence, ranking and operator card

Build an explainable candidate card, available in the dashboard and Telegram.

It must show:

- why-now trigger and strategy version;
- selected pair, liquidity, turnover and recent market movement;
- safety verdict, authority status, holder/insider signals and bounded deployer-history state;
- Jupiter buy/sell route result and price impact;
- attributed project/context evidence;
- explicit invalidation conditions;
- rank, hard-rejects and reasons;
- actions: `watch`, `shadow-pick`, `reject`.

No action signs or sends a transaction. Telegram commands only create an authenticated decision record.

## Durable outcome and replay loop

Outcome checks must be database rows, never `asyncio.sleep` tasks. On every scanner cycle:

1. claim a bounded batch of due rows transactionally;
2. obtain the appropriate quote/snapshot;
3. complete or fail the row with an explicit error;
4. retry only transient failures under a capped policy;
5. expose overdue and failed checks through `/healthz`.

Implement aggregate reports before candidate volume is increased. The report groups only complete outcomes by `strategy_version_id` and must include sample count, median net quote return, positive rate, worst result, exit-failure rate and missing-data rate.

Implement replay from stored `source_events`, `market_snapshots` and strategy-version configuration. Replay must not call live providers.

## Safety rules

- `RESEARCH` is the only permitted strategy state in this delivery.
- `RESEARCH_EXECUTION_ENABLED` defaults to `false`; any other value causes startup failure.
- No `SOLANA_PRIVATE_KEY` is required, read or accepted by the research process.
- A database-backed daily-loss breaker, hard slippage cap, maximum exposure, low-balance wallet and global kill switch are future `LIVE_CANDIDATE` requirements. Do not implement signing now.
- Include a visible global `research_paused` switch. It stops new scanning but continues durable outcome checks.
- Do not close token accounts in this delivery. That belongs only to a later supervised execution module after an account-state design review.

## Testing and acceptance criteria

### Automated

- Unit tests for configuration validation, provider budgets and RugCheck circuit breaker, advisory-lock standby mode, idempotent event insertion, `FOR UPDATE SKIP LOCKED` due-work claim, quote base-unit conversion/JSON string serialisation, unsafe-candidate short circuit and round-trip quote persistence.
- Migration test against an empty Neon-compatible Postgres instance.
- Migration test re-run is idempotent.
- Replay test uses fixtures only and performs zero network calls.
- API test verifies `/healthz` reports scanner `active` for the lock holder and `standby` for a second instance.
- Security test verifies research startup fails if execution is enabled or a private-key configuration is supplied.

### Manual

1. Deploy the Render service with a Neon database.
2. Confirm `/healthz` is green and the scanner is `active`.
3. Restart/redeploy and confirm the outgoing instance drains and explicitly releases its advisory lock; confirm the incoming process then becomes `active`. Also confirm a concurrent second process remains `standby` while the holder is healthy.
4. Insert a fixture source event and confirm raw event, market snapshot, safety result, buy quote, sell quote, decision card and durable outcome rows are visible.
5. Confirm Telegram creates a decision record but cannot trigger a swap.
6. Confirm a Render restart does not lose a pending outcome check.
7. Confirm no provider key, database URL or wallet material appears in logs, responses, fixtures or GitHub Actions output.

## PR sequence

1. **PR A: managed foundation** — Docker/Render, Neon configuration, Postgres repository/migrations, health, doctor, lock and tests.
2. **PR B: legacy archive and migration** — SQLite backup/export tool, legacy-cohort import, launch tracker/Telegram pause, state verdict changes supported by attached query output.
3. **PR C: provider prototypes and safety** — standalone bounded RugCheck, Helius and Jupiter quote helpers, budgets and fixtures.
4. **PR D: watch queue and durable outcomes** — candidate pipeline, outcome scheduler, replay and aggregate report.
5. **PR E: cards and operator workflow** — dashboard, Telegram cards and shadow-pick decisions.

Do not merge a PR unless its acceptance tests pass and its documentation explains any operator action required.

## Definition of done for the rebuild foundation

The foundation is complete when one Render service writes durable research records to Neon, only one scanner is active, restarts preserve due work, the dashboard exposes health and candidates, provider calls are bounded, Telegram can record a shadow-pick decision, and the system has no code path capable of signing or broadcasting a swap.
