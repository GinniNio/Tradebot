"""
find_zombies.py - Find old tokens showing v2-shaped revival signs.

Goal: surface candidates that LOOK LIKE the 5 winners from the 2026-05-28
forensic, not the 36 losers. The v1 selector was too permissive — it caught
every pop above +5%, including the FOMO blow-offs that subsequently collapsed.

Winner profile (from n=5 v1 winners on the 41 measured outcomes):
  - price_change_1h:  median +20.8%, max +49.2% — modest pops, never moonshots
  - volume_1h:        median $3,385, max $7,058 — quiet, not pump-volume loud
  - chain:            3 Solana, 2 Base (Base had 0 catastrophes in 5 signals)
  - liquidity:        median $15,982 — same floor as losers, not separating

Loser profile that this script must now reject:
  - price_change_1h reached +1,250% on the worst losers — FOMO blow-offs die
  - volume_1h median $10,774 — high-volume "revivals" are exit liquidity

Process:
  1. Gather a broad pool of tokens from each CHAINS chain (boosted + trending
     + search).
  2. For each, fetch current pair stats.
  3. Apply v2-shape filters:
       - pair_created_at >= MIN_PAIR_AGE_DAYS ago
       - liquidity_usd >= MIN_LIQUIDITY_USD
       - vol_24h <= MAX_VOLUME_24H_USD (not already raging)
       - vol_1h in [MIN_VOLUME_1H_USD, MAX_VOLUME_1H_USD] (quiet revival band)
       - price_change_1h in [MIN_PRICE_CHANGE_1H, MAX_PRICE_CHANGE_1H] (no moonshots)
       - vol_1h / (vol_24h / 24) >= MIN_VOLUME_SPIKE
  4. Score with Base preference and "winner-zone" centring for pc_1h / vol_1h.
  5. Write zombies.csv. Add chosen tokens via dashboard +Add Token.

Tunables here must stay aligned with config.ZOMBIE_V2_* — both ranges encode
the same hypothesis. If you change one, change the other.

Run:  python find_zombies.py

Uses only Dexscreener public API. No Helius credits consumed.
Expected runtime: ~90s (per chain).
"""

import argparse
import asyncio
import csv
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dexscreener_client import DexscreenerClient

logger = logging.getLogger(__name__)

# ─── Tunables — keep aligned with config.ZOMBIE_V2_* ──────────────────────────
CHAINS               = ["solana", "base"]   # base had 0 catastrophes in n=5
MIN_PAIR_AGE_DAYS    = 30        # token must have existed this long to qualify as "zombie"
MIN_LIQUIDITY_USD    = 15_000    # ignore illiquid tokens (matches risk_manager zombie floor)
MAX_LIQUIDITY_USD    = 1_500_000 # reject major established tokens (RAY/POPCAT/Pnut) — winners
                                 # capped at $54k; $1.5M leaves headroom for big Base memecoins
                                 # (TOSHI ~$1.16M) but catches multi-million-dollar majors
MAX_VOLUME_24H_USD   = 2_000_000 # raging tokens aren't reviving — already live
MIN_VOLUME_1H_USD    = 500       # need some real action right now (winner min was $484)
MAX_VOLUME_1H_USD    = 8_000     # v2: reject high-volume pumps (winner median $3,385 vs loser $10,774)
MIN_PRICE_CHANGE_1H  = 20.0      # v2: was 5.0 — winners all started above +20%
MAX_PRICE_CHANGE_1H  = 50.0      # v2: reject moonshots — losers reached +1,250%
MIN_VOLUME_SPIKE     = 2.0       # vol_1h must be Nx the hourly average

# --seed mode: discovery-time gate. Goal is to put winner-SHAPED tokens
# (quiet, mature, tradeable, right chain) into the watchlist so the bot's
# zombie_tracker catches the actual revival later. pc_1h and spike floors
# are effectively disabled — at SEED time the token doesn't need to be
# moving. Only the SHAPE matters. The pc_1h upper cap (50%) and vol_1h cap
# ($8k) stay — they're shape filters, not freshness filters.
SEED_MIN_PRICE_CHANGE_1H = -100.0  # accept any pc_1h (-100% floor never trips)
SEED_MIN_VOLUME_SPIKE    = 0.0     # accept any spike

# Centres for "winner zone" scoring. These are the medians from the v1 forensic.
WINNER_PC1H_CENTRE   = 21.0
WINNER_VOL1H_CENTRE  = 3_500.0

# Search queries to broaden the candidate pool (Dexscreener /search endpoint).
# Mix of generic (top pairs) + established memecoin/utility names to surface
# OLDER tokens that the boosted/profile lists miss. Per-chain seed lists below.
SEARCH_QUERIES_BY_CHAIN = {
    "solana": [
        "solana", "SOL/USDC", "SOL/USDT",
        "BONK", "WIF", "POPCAT", "JUP", "JTO", "RAY", "MEW", "PYTH",
        "BOME", "GIGA", "MOTHER", "PNUT", "ai16z",
    ],
    "base": [
        "base", "WETH/USDC", "USDC/WETH",
        "BRETT", "DEGEN", "TOSHI", "HIGHER", "AERO", "MOG", "MIGGLES",
        "NORMIE", "KEYCAT", "BOOMER", "DOGINME",
    ],
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def gather_candidate_tokens(dex: DexscreenerClient, chain: str) -> list[str]:
    """Pull a broad pool of token addresses for the given chain from multiple Dexscreener sources."""
    addresses: set[str] = set()

    # Source 1: currently boosted tokens
    try:
        boosted = await dex.get_boosted_tokens(chain, limit=50)
        for b in boosted:
            if ta := b.get("tokenAddress"):
                addresses.add(ta)
    except Exception as e:
        logger.warning("[%s] boosted fetch failed: %s", chain, e)

    # Source 2: token-profiles trending
    try:
        trending = await dex.get_trending_across_chains(chains=[chain])
        for t in trending:
            if ta := t.get("tokenAddress"):
                addresses.add(ta)
    except Exception as e:
        logger.warning("[%s] trending fetch failed: %s", chain, e)

    # Source 3: search results
    queries = SEARCH_QUERIES_BY_CHAIN.get(chain, [])
    for q in queries:
        try:
            pairs = await dex.search_tokens(q)
            for p in pairs[:30]:
                if (p.get("chainId") or "").lower() != chain:
                    continue
                base = (p.get("baseToken") or {}).get("address")
                if base:
                    addresses.add(base)
        except Exception as e:
            logger.warning("[%s] search '%s' failed: %s", chain, q, e)
        await asyncio.sleep(0.5)

    return list(addresses)


def days_since_unix_ms(unix_ms) -> int | None:
    if not unix_ms:
        return None
    try:
        ts = datetime.fromtimestamp(unix_ms / 1000, tz=timezone.utc)
        return (datetime.now(timezone.utc) - ts).days
    except Exception:
        return None


def zombie_score(spike: float, pc_1h: float, vol_1h: float, liq: float,
                 chain: str) -> float:
    """Higher = closer to the winner profile.

    v2 scoring rewards being IN the winner zone (modest pop, quiet volume)
    rather than maximizing pop magnitude or absolute volume. Outside the
    [MIN, MAX] gates the candidate is already rejected — score is for
    ranking the survivors.
    """
    score = 0.0

    # Reward proximity to winner medians. Linear falloff: 0 distance = full
    # credit, exceeding the half-window = 0 credit.
    pc1h_half_window  = (MAX_PRICE_CHANGE_1H - MIN_PRICE_CHANGE_1H) / 2  # 15
    vol1h_half_window = (MAX_VOLUME_1H_USD - MIN_VOLUME_1H_USD) / 2     # 3,750
    pc1h_proximity   = max(0, 1 - abs(pc_1h - WINNER_PC1H_CENTRE)   / pc1h_half_window)
    vol1h_proximity  = max(0, 1 - abs(vol_1h - WINNER_VOL1H_CENTRE) / vol1h_half_window)
    score += pc1h_proximity  * 25   # max +25
    score += vol1h_proximity * 25   # max +25

    # Spike still matters — it's the freshness signal. Capped lower than v1.
    score += min(spike, 10) * 1.5   # max +15

    # Chain preference: Base had 0 catastrophes in n=5.
    if chain == "base":
        score += 10
    elif chain == "solana":
        score += 0
    else:
        score -= 5  # unsupported chain — should never appear given CHAINS gate

    # Liquidity nudge — same direction as v1, much smaller weight.
    if liq < 20_000:
        score -= 3
    return round(score, 1)


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main(seed_mode: bool = False):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    pc_min = SEED_MIN_PRICE_CHANGE_1H if seed_mode else MIN_PRICE_CHANGE_1H
    sp_min = SEED_MIN_VOLUME_SPIKE    if seed_mode else MIN_VOLUME_SPIKE
    logger.info(
        "Mode: %s | pc_1h_min=%.0f%% | vol_1h cap=$%d | spike_min=%.1fx",
        "SEED (watchlist seeding)" if seed_mode else "FIRE (active revival)",
        pc_min, MAX_VOLUME_1H_USD, sp_min,
    )
    results = []
    near_misses = []
    async with DexscreenerClient() as dex:
        for chain in CHAINS:
            logger.info("[%s] Gathering candidate tokens from Dexscreener...", chain)
            addresses = await gather_candidate_tokens(dex, chain)
            if not addresses:
                logger.warning("[%s] No tokens fetched. Skipping.", chain)
                continue
            logger.info("[%s] Got %d unique candidate tokens to inspect", chain, len(addresses))

            for i, addr in enumerate(addresses, 1):
                stats = await dex.get_token_stats(addr, preferred_chain=chain)
                await asyncio.sleep(0.3)
                if not stats:
                    continue

                pair_age = days_since_unix_ms(stats.get("pair_created_at"))
                if not pair_age or pair_age < MIN_PAIR_AGE_DAYS:
                    continue

                vol_24h = stats.get("volume_24h", 0) or 0
                vol_1h  = stats.get("volume_1h", 0) or 0
                pc_1h   = stats.get("price_change_1h", 0) or 0
                liq     = stats.get("liquidity_usd", 0) or 0

                if liq < MIN_LIQUIDITY_USD or liq > MAX_LIQUIDITY_USD:
                    continue

                expected_hourly = vol_24h / 24 if vol_24h > 0 else 0
                spike = vol_1h / expected_hourly if expected_hourly > 0 else 0

                row = {
                    "score":         zombie_score(spike, pc_1h, vol_1h, liq, chain),
                    "chain":         chain,
                    "address":       addr,
                    "symbol":        stats.get("base_token") or "",
                    "pair_age_days": pair_age,
                    "vol_24h_usd":   round(vol_24h),
                    "vol_1h_usd":    round(vol_1h),
                    "vol_spike_x":   round(spike, 1),
                    "price_chg_1h":  round(pc_1h, 1),
                    "liquidity_usd": round(liq),
                    "dex_url":       stats.get("url") or "",
                }

                # v2 filters — both bounds matter
                fails = []
                if vol_24h > MAX_VOLUME_24H_USD:
                    fails.append(f"vol_24h ${round(vol_24h):,}>{MAX_VOLUME_24H_USD:,}")
                if vol_1h < MIN_VOLUME_1H_USD:
                    fails.append(f"vol_1h ${round(vol_1h):,}<{MIN_VOLUME_1H_USD:,}")
                if vol_1h > MAX_VOLUME_1H_USD:
                    fails.append(f"vol_1h ${round(vol_1h):,}>{MAX_VOLUME_1H_USD:,} (v2 cap)")
                if pc_1h < pc_min:
                    fails.append(f"pc_1h {pc_1h:.1f}%<{pc_min}%")
                if pc_1h > MAX_PRICE_CHANGE_1H:
                    fails.append(f"pc_1h {pc_1h:.1f}%>{MAX_PRICE_CHANGE_1H}% (v2 cap)")
                if spike < sp_min:
                    fails.append(f"spike {spike:.1f}x<{sp_min}x")

                if not fails:
                    results.append(row)
                    logger.info(
                        "[%s %d/%d] MATCH %s (%s): score=%.1f spike=%.1fx pc=%.1f%% vol1h=$%.0f age=%dd",
                        chain, i, len(addresses), row["symbol"] or addr[:8], addr[:8],
                        row["score"], spike, pc_1h, vol_1h, pair_age,
                    )
                else:
                    row["fail_reasons"] = "; ".join(fails)
                    near_misses.append(row)

    if not results:
        print()
        print("No FULL v2 zombie matches today (true revivals are rare).")
        if near_misses:
            near_misses.sort(key=lambda r: r["score"], reverse=True)
            print()
            print("Top 10 NEAR-MISSES (passed age + liquidity, failed other filters):")
            print(f"{'Chain':<8} {'Symbol':<10}  {'Age':>4}  {'Vol1h':>9}  {'Spike':>6}  {'PC1h':>6}  Why filtered out")
            print("-" * 100)
            for r in near_misses[:10]:
                print(
                    f"{r['chain']:<8} {(r['symbol'] or '?')[:10]:<10}  "
                    f"{r['pair_age_days']:>4}  ${r['vol_1h_usd']:>8,}  "
                    f"{r['vol_spike_x']:>5.1f}x  {r['price_chg_1h']:>5.1f}%  "
                    f"{r['fail_reasons']}"
                )
            print()
            print("Relax thresholds in find_zombies.py if any of these look promising.")
        else:
            print("Try again in a few hours, or relax MIN_PAIR_AGE_DAYS at the top of the file.")
        return

    results.sort(key=lambda r: r["score"], reverse=True)
    out_path = Path(__file__).parent / "zombies.csv"
    fieldnames = [
        "score", "chain", "address", "symbol", "pair_age_days", "vol_24h_usd",
        "vol_1h_usd", "vol_spike_x", "price_chg_1h", "liquidity_usd", "dex_url",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in results:
            w.writerow({k: r.get(k) for k in fieldnames})

    print()
    print(f"Wrote {len(results)} v2 zombie candidates to {out_path}")
    print()
    print(f"{'Score':>6}  {'Chain':<8} {'Symbol':<10}  {'Age':>4}  {'Vol1h':>9}  "
          f"{'Spike':>6}  {'PC1h':>6}  {'Liq':>9}")
    print("-" * 90)
    for r in results[:15]:
        print(
            f"{r['score']:>6.1f}  {r['chain']:<8} {(r['symbol'] or '?')[:10]:<10}  "
            f"{r['pair_age_days']:>4}  ${r['vol_1h_usd']:>8,}  "
            f"{r['vol_spike_x']:>5.1f}x  {r['price_chg_1h']:>5.1f}%  "
            f"${r['liquidity_usd']:>8,}"
        )
    print()
    print("Review zombies.csv, then add chosen tokens via the dashboard:")
    print("  http://localhost:3001  ->  Zombie Watchlist tab  ->  + Add Token")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else "")
    parser.add_argument(
        "--seed", action="store_true",
        help="Seed mode: relax pc_1h floor to 5%% and spike floor to 1.0x. "
             "Surfaces quiet dormant tokens to add to the watchlist. The bot's "
             "zombie_tracker enforces the strict v2 gate at signal-fire time."
    )
    args = parser.parse_args()
    asyncio.run(main(seed_mode=args.seed))
