# market_watch_v3: draft specification

**Status: DRAFT for the 2026-10-26 checkpoint. Not approved, not implemented.** Nothing here changes `market_watch_v2`, which keeps running unchanged until Kaye decides. Adopting any part of this means a new `STRATEGY_VERSION`, a fresh evaluation period, and no pooling with v2.

## Why

- v1 found 1 eligible token in 30 days. v2's discovery feed (`/token-profiles/latest/v1`) is almost all tokens under a day old, and most fail the $50k liquidity floor.
- A read-only check on 2026-10-20 scored every hour of the previous 30 days for the 57 tokens in CoinGecko's `solana-meme-coins` category with the frozen `evaluate()`. It used no returns.
  - 45 tokens passed all five hard filters at some point.
  - 10 reached 65 or more. Weekly first entries were 6, 1, 0, 2, 1.
  - 35 tokens reached 50–64, with weekly first entries of 24, 3, 2, 5, 1.
  - Caveats: the universe is survivors only, liquidity was held at its 2026-10-20 value, and the inputs came from hourly candles.
- Conclusion: at 65+ alone, an established-token universe cannot reach 30 tokens before the last 7-day admission date (2026-12-24). A pre-registered 50+ test can, if the universe keeps growing.

## Frozen (unchanged from v2)

- `tradebot/strategy.py` `evaluate()` and `SwingQualityConfig`, held by the golden test:
  - liquidity ≥ $50k;
  - 24h volume ≥ $100k;
  - market cap or FDV between $250k and $25M;
  - 24h change between +5% and +120%;
  - 1h change ≤ +25%;
  - score threshold 65.
- Horizons and tolerances: 1h (15 min), 24h (2 h), 3d (6 h), 7d (12 h). The primary horizon is 24h.
- Missing data:
  - `scheduled_at` never changes;
  - unresolved checks carry a reason;
  - no invented prices;
  - the conservative -100% scenario is reported separately.
- Cost scenarios (2% and 5%) and the SOL benchmark.
- No wallet and no execution path.

## Changes

### 1. Universe: separate address discovery from market data

**Market data and measurement come only from Dexscreener.** That covers:
- every snapshot;
- the pool age check (`pairCreatedAt`);
- the hard filters and score inputs;
- every outcome price.

CoinGecko and GeckoTerminal never supply a price, volume, market cap or liquidity.

**Address sources** supply mint addresses only:
- (a) tokens the bot already stored, once first seen 7 or more days ago (graduates);
- (b) Dexscreener `/token-boosts/latest/v1`, `/token-boosts/top/v1` and `/community-takeovers/latest/v1`;
- (c) optional: a once-daily list of Solana mints from CoinGecko's `solana-meme-coins` category.

Source (c) adds a provider. Doctrine rule 4 bars new providers during an evaluation, so (c) needs an explicit yes for v3.

**Admission to active tracking**, checked on Dexscreener:
- the deepest pool is at least 7 days old (`pairCreatedAt`);
- the deepest pool holds at least $25k of liquidity;
- the token passes the copycat check (below).

The $50k hard filter still applies at scoring.

**Removal from active tracking:** the token's deepest pool stays under $25k for 3 consecutive days. Its row is kept with a reason; nothing is deleted.

**Copycat check (details to settle):** exclude a token when another Solana mint with the same symbol (case-insensitive) has at least 10 times the deepest-pool liquidity, or an earlier `pairCreatedAt`. Exclusions are counted per cycle.

### 2. Inputs: Option A pool aggregation

Pools under $10k of liquidity are ignored.

| Input | Definition |
|---|---|
| `liquidity_usd` | Sum over the token's remaining pools (base side only) |
| `volume_1h/6h/24h_usd` | Sum over the same pools |
| `price_usd`, `price_change_1h/24h_pct`, `market_cap_usd`, `fdv_usd`, pool age | From the deepest pool |

Notes:
- **Fetching:** the batch endpoint `/tokens/v1/solana/{up to 30 mints}` returns one pool per token. This was checked on 2026-10-20: 1 pool returned against 30 from `/token-pairs/v1/solana/{mint}` for the same token. Option A therefore needs one `/token-pairs` call per token per cycle; a tracked universe of 300 tokens is 300 calls every 15 minutes. Confirm Dexscreener's published rate limit for that endpoint before fixing the universe cap.
- **Outcome prices** come from the enrolment's deepest pool, by pair address, as in v2.
- **The score code is unchanged, but the turnover input changes meaning.** Total volume divided by total liquidity is a different quantity from v2's single-pool figure, so v3 scores are not comparable with v2 scores.

### 3. Cohorts and enrolment

Enrolment is 100% in all tiers; there is no 50% sample.

| Cohort | Enrols at | Condition |
|---|---|---|
| `control` | the token's first observation that passes the hard filters and scores under 50 | no entry yet in a higher tier |
| `tier_50_64` | first observation that passes the hard filters and scores 50–64 | no `eligible` entry yet |
| `eligible` | first observation that passes the hard filters and scores 65+ | none |

- Each token is enrolled at most once per tier per version. The idempotency key `exp:{version}:{cohort}:{token}:{kind}` already includes the cohort.
- The "no higher tier yet" condition stops a token that has already peaked from adding a post-peak entry to a lower tier.

**Entry type**, decided and stored at enrolment for `tier_50_64` and `eligible`:
- `observed_crossing`: the tracking table holds an earlier observation of this token scoring under 50, whether or not it passed the hard filters.
- `first_seen_above`: the token's first v3 observation already scored 50 or more. This includes the start-up backlog and tokens that join later while already active.

### 4. Schema: migration 0005, additive only

- `outcome_checks`:
  - widen `ck_outcome_checks_cohort` to `('eligible','control','tier_50_64')`;
  - add `score_at_entry numeric(8,2)`;
  - add `entry_type text`, checked against the two values above or NULL.
- New `tracked_tokens` table:
  - token, strategy version, address source;
  - first seen at, first seen score;
  - last observation under 50;
  - last seen at;
  - deepest pool address and created-at;
  - copycat flag;
  - status, removed at, removed reason.
- This table also gives v3 the exact count of unique tokens assessed, which v2 cannot report.
- `score_at_entry` for v2 rows is reported by recomputing from the enrolment snapshot (`python -m tradebot.checkpoint`). It is never written back into v2 rows.

### 5. Report: pre-registered before any v3 data

- **Primary:** each token's first entry at 50+, meaning the earliest of its `tier_50_64` and `eligible` entries.
  - Compared with the `control` cohort and with SOL, at 24h.
  - Restricted to `observed_crossing` tokens.
  - The same test on `first_seen_above` tokens, and on both combined, is shown alongside.
- **Secondary:** `eligible` (65+) on its own, and `tier_50_64` on its own.
- **Descriptive only, labelled as conditioned on the future:** the earlier and later entries of tokens that reached both tiers. The earlier entry's return includes the rise that took the token to 65.
- **Overlap:** tokens in both `control` and a 50+ tier are listed. Control is never redefined as "never reached 50", because that would use the future.
- **Promising** means the same five tests as v2, applied to the primary comparison:
  1. median and mean both positive after costs;
  2. both beat SOL and control;
  3. the result survives dropping the top one or two tokens;
  4. it holds in both chronological halves;
  5. at least 30 tokens in the primary group.

### 6. Calendar

- If approved on 2026-10-26, v3 runs from its deploy date.
- 7-day cohorts must be admitted by 2026-12-24.
- The kill-criterion decision is on 2026-12-31.
- Whether v2's own discovery keeps running is an open decision. If it stops, v2's pending checks still complete.

## Decisions for Kaye at the checkpoint

1. Adopt v3 at all. This depends on the checkpoint showing that v2's new-token feed still supplies almost no qualifying tokens.
2. Approve the primary test as first entry at 50+ (`observed_crossing`) against control and SOL at 24h. This is a new hypothesis. It does not loosen v2, which is closed and reported as it stands.
3. Approve CoinGecko as an address-only source (c), or not.
4. Fix the numbers for: admission (pool age 7d, $25k), removal (3 days under $25k), dust ($10k), and the copycat thresholds.
5. Stop or keep v2 discovery.

## Evidence to read first

Both commands are read-only:

- `python -m tradebot.verdict --days 7`: v2 scan health, throughput, rejection reasons and outcomes.
- `python -m tradebot.checkpoint --days 7 --graduates`:
  - score bands of stored hard-filter passers;
  - control score at enrolment;
  - the size of the graduate pool: how many tokens first seen 7 or more days ago still have pools, pass the hard filters now under Option A, and in which band.
  - This is a single snapshot. The 7–14 day bucket is the closest available proxy for weekly inflow.
