# Tradebot: Solo Operator Scope

## Purpose

Tradebot is a Solana candidate-intelligence system. It scans the market, gathers attributable evidence, measures outcomes and presents a short ranked list for one operator to review.

It does not claim to predict profit and it does not autonomously trade real money.

## Operating loop

1. **Scan**: capture new and changing Solana candidates from managed data feeds. A discovery event puts a token in a short-lived watch queue; it is never a buy signal.
2. **Check**: confirm pair, liquidity, turnover, price behaviour, age and current Jupiter buy-and-sell quotes at the research size. Reject a token when an exit cannot be quoted.
3. **Research**: collect only attributable evidence: official links, project releases, verified announcements, on-chain activity and wallet behaviour. Each fact retains its source, source time and fetch time.
4. **Rank**: create an explainable candidate card with market state, hard-reject flags, evidence, invalidation conditions and comparable past observations. Output up to five candidates, or none.
5. **Pick**: the operator accepts, rejects or watches a candidate. The decision and reason are stored. A pick creates a shadow position only.
6. **Learn**: reprice accepted, rejected and watched candidates on the same schedule. Compare cohorts using net executable quotes before changing a rule.

## Explicit exclusions

- First-seconds launch sniping, MEV, Jito/Shredstream competition and latency races.
- Automatic copy trading or automatic real-money execution.
- Direct pool-contract swaps as the default path.
- CEX market making, arbitrage contracts and custody of other people's funds.
- Broad web crawling, untraceable sentiment scores or social-post-driven buys.
- Multi-chain expansion before Solana research has shown a repeatable result.

## Minimal infrastructure

| Service | Responsibility |
|---|---|
| GitHub | Source control, pull-request review, CI and release history. |
| Render | One single-instance Python service containing the API, dashboard and scheduled research loop. A database lease prevents concurrent scanners. |
| Neon Postgres | Operational data: raw source events, market snapshots, evidence, decisions, shadow quotes and outcome checks. |
| Dexscreener | Broad discovery and market validation. Use streaming updates where available and REST only to enrich queued tokens. |
| Helius | Targeted Solana enrichment for the candidate and wallet watchlists. |
| Jupiter | Read-only buy and sell quotes for tradability and shadow execution. Future execution remains disabled. |
| Telegram | Operator alerts and concise candidate cards. It is not an execution authority. |

No VPS fleet, self-hosted node, Kafka, Redis, MongoDB, Jito infrastructure or smart contract is required.

## Data records

| Record | Minimum contents |
|---|---|
| `source_event` | provider, raw payload, event and fetch timestamps, token and pair identifiers |
| `market_snapshot` | selected pair, liquidity, volume, price changes and transaction counts |
| `quote_snapshot` | buy and sell quote for a fixed research size, price impact, route and failure reason |
| `evidence_item` | claim, source URL or transaction, publisher, source time, fetch time and classification |
| `candidate_decision` | rank, reasons, hard-reject flags, operator action and notes |
| `outcome_check` | scheduled quote-based value, completion state and error reason |

Raw inputs are immutable. Derived scores carry a version so old results can be replayed against the rules that selected them.

## Operator routine

**Boot:** run `doctor`: database and migrations, provider credentials, feed freshness, scanner lease, dashboard health and alert delivery. A failed required check pauses scanning and sends one alert.

**During the day:** review only cards that passed hard rejects. Silence is the default; alerts are for newly ranked candidates and failed health checks.

**Daily:** record a decision for each shortlisted candidate and inspect failed outcome checks. No forced picks.

**Weekly:** review frozen strategy versions and complete cohorts: median net quote return, positive rate, downside, exit failures and missing-data rate. Stop or version a weak selector; preserve the old evidence.

## Promotion policy

`RESEARCH` is the only permitted strategy state. A later paper-trading proposal requires a frozen selector, completed out-of-sample cohort, quote-based net performance, a defined loss cap and an operator-approved change request. Live trading is a separate product decision.

## Build order

1. Align the current database and documentation with this scope.
2. Move operational data to Neon and deploy the single Render instance with a health endpoint and scanner lease.
3. Add immutable event, quote and decision records.
4. Replace the hand-built launch scanner with the watch queue and hard-reject pipeline.
5. Add evidence cards and Telegram alerts.
6. Build replay and cohort reporting before adding strategies.
