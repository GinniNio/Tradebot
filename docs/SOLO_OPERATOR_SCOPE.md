# Tradebot Rebuild Scope & Mission

## Mission

Tradebot is an asynchronous Solana opportunity-research pipeline for one operator. It scans, scrapes, analyses, researches and ranks candidates, then presents clear picks for human review.

It is not a latency-dependent execution bot. It abandons minute-one launch sniping and excludes arbitrage smart-contract templates and copied auto-trading bots. Strategies begin as verifiable hypotheses, including post-catalyst continuation and independent wallet convergence. Each hypothesis is measured before its state can change.

A valid outcome is a shortlist, a watchlist or no pick. The system does not claim to predict profit.

## Product boundary

### In scope

- Solana tokens with a liquid, observable secondary market.
- Market discovery, attributable context research, bounded wallet/deployer analysis, token-safety checks and route-based exitability checks.
- Explainable ranked cards and stored operator decisions.
- Shadow positions and outcome research using executable buy and sell quotes.
- A future supervised-promotion path governed by the state machine.

### Out of scope

- First-seconds launch sniping, MEV, Jito/Shredstream competition and latency races.
- Automatic copy trading, automatic real-money execution or autonomous position management.
- Direct pool-contract swaps as the default path.
- CEX market making, arbitrage contracts and custody of other people's funds.
- Broad web crawling, untraceable sentiment scores or social-post-driven buys.
- Multi-chain expansion before the Solana research loop has shown a repeatable result.

## Legacy strategy disposition

The rebuild has one post-launch watch-queue discovery mode. It does not carry forward dormant-token revival or minute-one launch discovery.

- `launch_momentum` is a legacy measurement strategy and must be formally moved to `STOP` in the state-machine change set. Its current verdict is recorded separately from this scope document.
- `zombie_revival` remains terminal `STOP`.
- `zombie_revival_v2` remains `RESEARCH` only until its 49-outcome forensic pass is completed. It receives no new discovery work and cannot be promoted in the meantime.
- Legacy outcome data remains available as a control cohort. It is not mixed with the new watch-queue strategy.

## Minimal infrastructure architecture

| Service | Responsibility |
|---|---|
| GitHub | Repository, pull-request review, Actions for linting, migration tests and Render deployment. No production secret enters the repository or Actions log. |
| Neon Postgres | Durable source events, snapshots, evidence, decisions, shadow quotes and outcome checks. It replaces local SQLite because Render disk is not durable operational storage. |
| Render | One single-instance FastAPI service: dashboard, API and asynchronous background loops. A session-level Postgres advisory lock allows exactly one active scanner across deployment overlap and restart. |
| Dexscreener | Broad discovery and market validation. Streaming updates where available; REST only to enrich queued tokens. |
| RugCheck | First token-safety call: risk, liquidity, holder and insider signals. A failed or unavailable verdict is explicit; it is never silently treated as safe. |
| Helius | One bounded metadata/enrichment call for candidates that passed initial safety. It is not used to recursively reconstruct creator histories. |
| Jupiter | Read-only bidirectional quotes for tradability and shadow execution. The execution adapter remains disabled in the initial build. |
| Telegram | Candidate cards, health alerts and future supervised approval prompts. It is not an execution authority. |

No VPS fleet, self-hosted node, Kafka, Redis, MongoDB, Jito infrastructure or smart contract is required.

## Pipeline mechanics

### 1. Scan: fast discovery

Monitor managed market feeds for candidates that meet the baseline `swing_quality` liquidity and sustained-volume rules. A discovery event creates a short-lived watch-queue entry. It is never a buy signal.

### 2. Scrape: attributable context

Collect token metadata, official links and attributable public evidence. Creator history is initially a bounded enrichment: RugCheck safety/insider data plus one Helius metadata lookup. A missing or inconclusive creator-history result is stored as `unknown`; the pipeline does not recursively walk transaction signatures.

### 3. Analyse: safety before quoting

Before calling Jupiter:

- select the actual trading pair;
- check mint authority and freeze authority where applicable;
- assess holder concentration, RugCheck verdict and available deployer/insider signals;
- apply explicit provider rate and concurrency budgets.

Only a candidate that clears hard safety rejects reaches the quote stage.

### 4. Validate exitability: bidirectional quote

For each eligible candidate, request a Jupiter buy quote at each fixed research size, initially 0.5 SOL and 1 SOL. Take the resulting token base-unit amount from the buy quote and submit that exact amount to a sell quote back to SOL.

Record the pair, both routes, quoted outputs, price impact, route failures and round-trip loss. Reject a token when a realistic exit cannot be quoted.

### 5. Research and rank

Attach only evidence that can be reviewed: source URL or transaction, publisher, source time, fetch time, classification and claim. A project announcement can support a hypothesis; it cannot override failed safety or exitability.

Create a candidate card with market state, hard-reject flags, evidence, invalidation conditions and comparable past observations. The shortlist contains at most five candidates, or none.

### 6. Pick and learn

The operator records `watch`, `shadow-pick` or `reject`, with a reason. A shadow pick never signs or broadcasts a transaction.

Accepted, rejected and watched candidates are repriced on a common durable schedule. Compare cohorts using net executable quote returns, outcome completeness, median return, positive rate, downside and exit failures. Raw inputs are immutable; derived scores carry a version.

## Operational controls

### Durable scheduling and deployment

Every outcome check is a Neon row with a target execution timestamp and status. The scanner queries due work after each loop and after every restart. It does not rely on in-memory `asyncio.sleep` timers.

The scanner holds a session-level Postgres advisory lock for its entire worker connection. A new Render instance fails closed if it cannot acquire that lock. It must not use a timestamp lease.

### Provider budgets

Provider concurrency, timeout, retry and request-budget values are explicit environment configuration. Each client has bounded queues and exponential backoff for HTTP 429 and transient failure. A rate-limited provider degrades that evidence field to `unavailable`; it does not increase scanner concurrency.

### Data integrity

Every `candidate_decision` and `quote_snapshot` stores the strategy version and exact Git commit SHA. Price-bearing rows enforce non-negative values and a configured minimum price floor before return calculation. Outcome calculations reject missing, zero and dust-price inputs instead of dividing them.

## Operator routine

**Boot:** run `doctor`: database and migration status, provider credentials, provider budget configuration, feed freshness, advisory-lock status, dashboard health and alert delivery. A failed required check pauses scanning and sends one health alert.

**During the day:** alerts are reserved for a new ranked candidate or health failure. The operator sees the full candidate card before recording any decision.

**Daily:** review the shortlist, store a decision for each candidate and resolve failed outcome checks. No forced picks.

**Weekly:** review frozen strategy versions and complete cohorts. Stop or version a weak selector; preserve the original evidence.

## State machine and capital controls

The strategy states remain:

`RESEARCH → PAPER → LIVE_CANDIDATE → LIVE`

The first delivery permits `RESEARCH` only. It gathers data and creates shadow positions; it cannot sign or send a transaction.

A `PAPER` proposal requires a frozen selector, out-of-sample cohort, quote-based net-performance report and an operator-approved change request.

A `LIVE_CANDIDATE` proposal additionally requires database-backed daily-loss circuit breakers, hard slippage caps, maximum exposure, a tested global kill switch and a dedicated low-balance wallet.

A later supervised `LIVE` proposal additionally requires one-tap Telegram approval displaying the candidate card before signing and safe closure of unused Solana token accounts after an approved exit. Live trading remains a separate product decision.

## Delivery sequence

0. Validate the bounded RugCheck, Helius and bidirectional-Jupiter helpers in standalone scripts, with recorded sample responses and failure cases.
1. Move the operational schema to Neon and deploy the single Render instance with GitHub Actions checks, health endpoint and advisory-lock worker.
2. Add immutable source-event, market-snapshot, quote-snapshot, strategy-version, decision and durable-outcome records. Implement aggregation and replay here, before the watch queue produces candidates.
3. Apply the legacy strategy state changes: archive `launch_momentum`; complete the `zombie_revival_v2` forensic and then archive or formally replace it under a new strategy key.
4. Implement the managed watch queue, hard-reject pipeline, provider budgets and context evidence.
5. Add candidate cards, Telegram alerts and shadow picks.
6. Review the first frozen cohort before adding any new strategy or execution capability.
