# Tradebot Rebuild Scope & Mission

## Mission

Tradebot is an asynchronous Solana opportunity-research pipeline for one operator. It scans, scrapes, analyses, researches and ranks candidates, then presents clear picks for human review.

It is not a latency-dependent execution bot. It abandons minute-one launch sniping and does not use arbitrage smart-contract templates or copied auto-trading bots. Strategies start as verifiable hypotheses, such as post-catalyst continuation or independent wallet convergence. Each hypothesis is measured objectively before it earns a change in state.

A valid outcome is a shortlist, a watchlist or no pick. The system does not claim to predict profit.

## Product boundary

### In scope

- Solana candidates with a liquid, observable secondary market.
- Market discovery, structured project/context research, wallet and deployer analysis, security checks and route-based exitability checks.
- Explainable ranked candidate cards and stored operator decisions.
- Shadow positions and outcome research using executable buy and sell quotes.
- A future supervised promotion path governed by the existing state machine.

### Out of scope

- First-seconds launch sniping, MEV, Jito/Shredstream competition and latency races.
- Automatic copy trading, automatic real-money execution or autonomous position management.
- Direct pool-contract swaps as the default path.
- CEX market making, arbitrage contracts and custody of other people's funds.
- Broad web crawling, untraceable sentiment scores or social-post-driven buys.
- Multi-chain expansion before the Solana research loop has shown a repeatable result.

## Minimal infrastructure architecture

| Service | Responsibility |
|---|---|
| GitHub | Repository, pull-request review, Actions for linting, schema tests and Render deployment. No production secret enters the repository or Actions log. |
| Neon Postgres | Durable source events, snapshots, evidence, decisions, shadow quotes and outcome checks. It replaces local SQLite because Render disk is not durable operational storage. |
| Render | One single-instance FastAPI service: dashboard, API and asynchronous background loops. A Postgres scanner lease prevents overlap after a restart or deployment. |
| Dexscreener | Broad candidate discovery and market validation. Streaming updates where available; REST only to enrich queued tokens. |
| Helius | Targeted Solana enrichment for the small candidate and wallet watchlists. It is not a broad polling feed. |
| Jupiter | Read-only bidirectional quotes for tradability and shadow execution. The execution adapter remains disabled in the initial build. |
| Telegram | Candidate cards, health alerts and future supervised approval prompts. It is never a standalone execution authority. |

No VPS fleet, self-hosted node, Kafka, Redis, MongoDB, Jito infrastructure or smart contract is required.

## Pipeline mechanics

### 1. Scan: fast discovery

Monitor managed market feeds for candidates that meet the baseline `swing_quality` liquidity and sustained-volume rules. A discovery event creates a short-lived watch queue entry. It is never a buy signal.

### 2. Scrape: contextual enrichment

Collect token metadata, official links and attributable public evidence. Add a deployer-history module that links prior token launches, liquidity-removal behaviour and known negative signals where data supports them. Store raw sources and timestamps with every extracted fact.

### 3. Analyse: safety and exitability

For each queued candidate:

- select the actual trading pair;
- assess liquidity, turnover, price structure and holder concentration;
- check mint and freeze authority where applicable;
- evaluate deployer and wallet history;
- request Jupiter buy and sell quotes at fixed research sizes, initially 0.5 SOL and 1 SOL;
- record route, quoted output, price impact and failure reason;
- hard-reject a candidate where a realistic exit cannot be quoted.

### 4. Research: evidence and hypothesis

Attach only evidence that can be reviewed: source URL or transaction, publisher, source time, fetch time, classification and claim. A project announcement can support a hypothesis. It cannot override a failed safety or exitability check.

### 5. Pick: decision card

Send a concise Telegram and dashboard card for shortlisted candidates:

| Field | Required content |
|---|---|
| Why now | Trigger, strategy version and supporting market changes |
| Entry context | Selected pair, liquidity, volume and entry conditions |
| Exitability | Bidirectional quote, route quality and price impact |
| Safety | Authority, holder and deployer-history verdicts |
| Evidence | Attributable links and independent confirmation count |
| Invalidation | Conditions that make the thesis false or the position unsafe |
| Operator action | Watch, shadow-pick or reject, with a stored reason |

The shortlist contains at most five candidates. No forced picks.

### 6. Learn: outcome research

Reprice accepted, rejected and watched candidates on a common schedule. Review results using net executable quote returns, outcome completeness, median return, positive rate, downside and exit failures. Derived scores are versioned; raw inputs remain immutable.

## Operational workflow

### Boot

Run `doctor` before scanning: database connection and migrations, provider credentials, feed freshness, scanner lease, dashboard health and alert delivery. A failed required check pauses scanning and raises one health alert.

### During the day

The operator sees alerts only for a new ranked candidate or a health failure. Research and decision records are written before a shadow position is opened.

### Daily

Review the shortlist, store a decision for each candidate and resolve failed outcome checks.

### Weekly

Review frozen strategy versions and complete cohorts. A weak cohort is stopped or revised under a new version. Its previous evidence remains unchanged.

## State machine and capital controls

The strategy states remain:

`RESEARCH → PAPER → LIVE_CANDIDATE → LIVE`

The first delivery permits `RESEARCH` only. It gathers data and creates shadow positions; it cannot sign or send a transaction.

Any later `PAPER` or supervised live proposal must include:

- a frozen selector and completed out-of-sample evidence;
- database-backed daily-loss circuit breakers;
- hard slippage caps validated against current Jupiter quotes;
- an explicit maximum exposure and an independently tested global kill switch;
- a dedicated low-balance wallet;
- one-tap Telegram approval that shows the candidate card before signing;
- automatic closure of unused Solana token accounts after an approved position exit, subject to account-state checks.

Real-money execution is a separate approval and implementation decision.

## Operational records

| Record | Minimum contents |
|---|---|
| `source_event` | provider, raw payload, event and fetch timestamps, token and pair identifiers |
| `market_snapshot` | pair, liquidity, volume, price changes and transaction counts |
| `quote_snapshot` | buy and sell quote, price impact, route and failure reason |
| `evidence_item` | claim, source URL or transaction, publisher, source and fetch times |
| `candidate_decision` | rank, hard-reject flags, operator action and notes |
| `outcome_check` | scheduled quote-based value, status and error reason |
| `strategy_version` | immutable configuration and code/version reference used for the decision |

## Delivery sequence

1. Move the operational schema to Neon and deploy the single Render instance with health endpoint, scanner lease and GitHub Actions checks.
2. Add immutable source-event, market-snapshot, quote-snapshot and operator-decision records.
3. Implement the deployer-history and safety/exitability modules.
4. Replace the hand-built launch scanner with the managed watch queue and hard-reject pipeline.
5. Add evidence cards, Telegram alerts and shadow picks.
6. Build replay and cohort reporting before adding strategies or execution.
