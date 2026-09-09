# How Tradebot Works

> **Authoritative scope:** Tradebot is a research-only Solana candidate-intelligence system. It scans, checks, researches, ranks and records operator picks as shadow positions. It does not autonomously trade real money. See [Solo Operator Scope](docs/SOLO_OPERATOR_SCOPE.md).

Research-only paper-trading bot for Solana memecoins. No position has ever opened. This doc covers how it finds candidates, decides whether to signal, and how it measures whether a strategy is any good.

## The core loop

Three trackers run independently, each polling a different source. When a tracker's filters pass, it fires a signal. Every signal gets logged with an outcome row and four scheduled price checks (+5m, +15m, +1h, +24h). A state-machine gate sits between "signal fired" and "position opened" — right now that gate blocks everything, because every strategy is in RESEARCH, STOP, or PAUSED. Nothing is tradable.

```
tracker polls source → filters candidate → create_signal() in DB
  → record_signal_outcome() (outcome row + 4 pending price checks)
  → main.handle_signal()
  → risk_manager.can_open_position() — STATE GATE, blocks all RESEARCH/STOP/PAUSED strategies
  → (never reached) jupiter_client.buy_token()
```

A background loop wakes every 60s, finds due price checks, and fills them via batched Dexscreener calls. This is DB-backed so it survives restarts — a check that comes due while the bot is down still gets filled once it's back up.

## The three signal sources

### 1. Wallet tracker — PAUSED
Polls a set of tracked "smart" wallets via Helius. If `WALLET_TRIGGER_COUNT` (6) distinct wallets buy the same token within `WALLET_LOOKBACK_MINUTES`, that's convergence — fires a wallet signal. Disabled since Helius free-tier quota ran out (resets 2026-06-20; a scheduled task flips it back on automatically). Known weakness: tracked wallets often move in lockstep because they're sniper bots, not independent actors — trigger=6 mutes most of that but doesn't fix the underlying wallet set.

### 2. Zombie tracker — mixed states
Watches dormant tokens (no volume ≥14 days) for revival. Runs Dexscreener-only (Birdeye is too small a plan to sustain per-token dormancy polling).

Two selectors run in parallel over the same watchlist:
- **zombie_revival (v1)** — fires on any revival signal. **STOP as of 2026-05-28.** Verdict on n=41: median 24h return -77%, only 12% positive. Early shape was a pop-and-die: +5% at 5m, +14% at 15m, then collapse by 1h. No feature at signal time separated the 12% real revivals from the 88% dead-cat bounces — the selector itself was wrong, not the exit timing. Terminal state; needs a new selector and new strategy key to reopen.
- **zombie_revival_v2** — tightened selector applied at signal-fire time: `price_change_1h` in [20%, 50%] (rejects blow-off tops), `volume_1h` ≤ $8,000 (prefers quiet revivals), chain in {base, solana} (Base had zero catastrophic losses in the v1 forensic). Derived from forensic on the same 41 v1 outcomes — winners were quiet, losers were loud. Registered RESEARCH 2026-05-28, in-sample by construction; needs a genuinely fresh out-of-sample read before anyone trusts it.

### 3. Graduation tracker (launch_momentum) — RESEARCH
Added 2026-06-05. Polls pump.fun's public API every 60s for tokens that just graduated off the bonding curve (`complete=True`), enriches each with Dexscreener stats, and fires if it clears: liquidity ≥ $15k, market cap ≤ $500k, volume_1h ≥ $2k, age ≤ 15 min. Detection lag ~1-2 min behind the actual graduation event (a Helius websocket would tighten this but that's blocked on the same June-20 quota reset).

## How a signal turns into a verdict

Every fired signal writes one row to `signal_outcomes` with `price_at_signal` and a route-availability check (was there actually a Jupiter route at signal time — separates "no edge" from "no execution path"). The four scheduled checks backfill `price_5m/15m/1h/24h` as they come due.

The verdict query aggregates per strategy:

```sql
SELECT strategy, COUNT(*) AS signals,
       AVG(CAST(route_available_at_signal AS REAL)) AS route_rate,
       AVG(price_5m/price_at_signal - 1)  AS avg_5m,
       AVG(price_15m/price_at_signal - 1) AS avg_15m,
       AVG(price_1h/price_at_signal - 1)  AS avg_1h,
       AVG(price_24h/price_at_signal - 1) AS avg_24h
FROM signal_outcomes
WHERE source = 'live' AND price_at_signal IS NOT NULL
GROUP BY strategy;
```

Run once a strategy hits ~30-50 outcomes. This is a human-reviewed gate, not automatic — Kaye looks at the mean/median spread (median worse than mean = systemic decay across most tokens, not a few blown-up rugs skewing the mean), the positive-rate, and the early-window shape (does it pop and die, or decay from minute one) before deciding whether to promote, tighten the selector, or archive to STOP.

Promotion ladder: RESEARCH → PAPER (30-50 outcomes) → LIVE_CANDIDATE (100 paper trades) → LIVE (250+ trades, manual review, kill switch). STOP is terminal for that strategy key.

## Current read (as of 2026-07-19)

launch_momentum crossed its verdict threshold (n=561, 492 with 24h price filled) and the shape looks like a STOP candidate: route_rate 99.8% (execution isn't the issue), avg_24h -48.1%, only 6.5% positive at 24h, and no early pop to distinguish it from zombie v1 — it just decays from minute one. Not yet archived; needs the same human review zombie v1 got before any config change.

## Why the gate exists

The state machine in `risk_manager.can_open_position()` checks strategy state *first*, before liquidity, balance, or any other filter. This is deliberate: a signal is data until a strategy has enough logged outcomes to show it's not just noise. Nothing gets promoted without someone running the verdict query and reading the result — not the bot, not an automatic threshold.
