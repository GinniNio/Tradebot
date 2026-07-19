"""
find_wallets.py - Discover candidate smart wallets for the bot to track.

Process:
  1. Pull trending Solana tokens from Dexscreener (free, no key).
  2. For each token, find its most liquid pair, then fetch recent SWAP transactions
     against that pool via Helius Enhanced Transactions API.
  3. Tally which wallets bought across multiple tokens (cross-token = generalist trader).
  4. For wallets appearing in >= MIN_CROSS_TOKEN tokens, pull their last N swaps and
     compute: trade count, token diversity, wallet age (from oldest swap), bot score.
  5. Look up each candidate's SOL balance via Helius RPC.
  6. Rank by composite_score and write candidates.csv.

Review candidates.csv, then add the wallets you like via the dashboard's
"+ Add Wallet" button.

Run:  python find_wallets.py

Helius credit budget per run: ~500-1500 (free tier is 1M/month).
Expected runtime: 60-180 seconds.
"""

import asyncio
import csv
import logging
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).parent))
from config import HELIUS_API_KEY
from helius_client import HeliusClient
from dexscreener_client import DexscreenerClient

logger = logging.getLogger(__name__)

# ─── Tunables ─────────────────────────────────────────────────────────────────
TRENDING_LIMIT     = 30    # how many trending Solana tokens to scan
SWAPS_PER_TOKEN    = 50    # how many recent pool swaps to inspect per token
MIN_CROSS_TOKEN    = 2     # wallet must appear as buyer in this many tokens
SWAPS_PER_WALLET   = 100   # how deep to look back per candidate
MAX_CANDIDATES     = 80    # cap on wallets we vet in detail (credit guard)

SKIP_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "So11111111111111111111111111111111111111112",   # wSOL
}

HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def get_trending_tokens(dex: DexscreenerClient) -> list[str]:
    """Pull boosted Solana tokens from Dexscreener — proxy for 'getting attention'."""
    boosted = await dex.get_boosted_tokens("solana", limit=TRENDING_LIMIT)
    addrs = []
    for b in boosted:
        ta = b.get("tokenAddress")
        if ta:
            addrs.append(ta)
    # Dedup preserving order
    return list(dict.fromkeys(addrs))[:TRENDING_LIMIT]


async def get_pair_address(dex: DexscreenerClient, token_address: str) -> str | None:
    """Pick the most liquid Solana pair for the token."""
    pairs = await dex.get_pairs_by_token(token_address)
    sol_pairs = [p for p in pairs if (p.get("chainId") or "").lower() == "solana"]
    if not sol_pairs:
        return None
    sol_pairs.sort(
        key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0),
        reverse=True,
    )
    return sol_pairs[0].get("pairAddress")


async def get_token_buyers(helius: HeliusClient, pair_address: str,
                            token_address: str) -> set[str]:
    """
    Fetch recent SWAPs against the pool. A wallet that RECEIVED the target mint
    counts as a buyer.
    """
    txs = await helius.get_address_transactions(
        pair_address, tx_type="SWAP", limit=SWAPS_PER_TOKEN
    )
    buyers = set()
    for tx in txs:
        for t in tx.get("tokenTransfers") or []:
            if t.get("mint") != token_address:
                continue
            buyer = t.get("toUserAccount")
            if buyer and buyer != pair_address:
                buyers.add(buyer)
    return buyers


async def get_sol_balance(rpc: aiohttp.ClientSession, wallet: str) -> float:
    """Standard Solana JSON-RPC getBalance via Helius RPC endpoint."""
    try:
        async with rpc.post(
            HELIUS_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet]},
        ) as r:
            data = await r.json()
            lamports = ((data or {}).get("result") or {}).get("value") or 0
            return lamports / 1_000_000_000
    except Exception as e:
        logger.warning("getBalance failed for %s: %s", wallet[:8], e)
        return 0.0


async def get_oldest_signature_ts(rpc: aiohttp.ClientSession, wallet: str,
                                   max_pages: int = 3) -> datetime | None:
    """
    Paginate getSignaturesForAddress (Helius RPC, 1000 sigs/page) to estimate the
    wallet's oldest activity. Returns the timestamp of the oldest signature found
    within max_pages * 1000 signatures. For wallets older than that, this is a
    lower bound (real age is at least this old).
    """
    before = None
    oldest_ts: datetime | None = None
    for _ in range(max_pages):
        opts = {"limit": 1000}
        if before:
            opts["before"] = before
        try:
            async with rpc.post(HELIUS_RPC, json={
                "jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                "params": [wallet, opts],
            }) as r:
                data = await r.json()
                sigs = (data or {}).get("result") or []
                if not sigs:
                    break
                # Sigs come back newest-first. Iterate from the oldest end and grab
                # the first non-null blockTime (some old slots return null).
                for sig in reversed(sigs):
                    bt = sig.get("blockTime")
                    if bt:
                        oldest_ts = datetime.fromtimestamp(bt, tz=timezone.utc)
                        break
                if len(sigs) < 1000:
                    break
                before = sigs[-1].get("signature")
                await asyncio.sleep(0.3)
        except Exception as e:
            logger.warning("getSignaturesForAddress failed for %s: %s", wallet[:8], e)
            break
    return oldest_ts


def categorize_size(sol_balance: float) -> str:
    if sol_balance < 100:    return "small (<100 SOL)"
    if sol_balance < 1000:   return "mid (100-1000 SOL)"
    if sol_balance < 10000:  return "large (1k-10k SOL)"
    return "whale (>10k SOL)"


def vet_wallet(swaps: list[dict], cross_token_count: int,
               wallet_age_days: int) -> dict | None:
    """Compute vetting metrics from a wallet's swap history + externally-determined age."""
    if not swaps:
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    distinct_mints: set[str] = set()
    swap_count_30d = 0

    for tx in swaps:
        ts_unix = tx.get("timestamp")
        if not ts_unix:
            continue
        ts = datetime.fromtimestamp(ts_unix, tz=timezone.utc)
        if ts >= cutoff:
            swap_count_30d += 1
        for t in tx.get("tokenTransfers") or []:
            mint = t.get("mint")
            if mint and mint not in SKIP_MINTS:
                distinct_mints.add(mint)

    # Bot detection. Sniper bots in trending tokens look like:
    #   - cross-token appearances >= 8 (buys ~every trending token)
    #   - >20 swaps/day average (well above human deliberate trading)
    #   - many distinct mints touched in a tiny age window (no holding period)
    bot_score = 0
    if cross_token_count >= 8:
        bot_score += 3
    elif cross_token_count >= 5:
        bot_score += 1

    swaps_per_day = swap_count_30d / 30 if swap_count_30d else 0
    if swaps_per_day > 20:
        bot_score += 3
    elif swaps_per_day > 10:
        bot_score += 1

    # "100 tokens in 1 day" pattern: extreme mint churn relative to age
    if wallet_age_days <= 3 and len(distinct_mints) > 30:
        bot_score += 3
    elif wallet_age_days <= 7 and len(distinct_mints) > 50:
        bot_score += 2

    return {
        "swap_count_30d":          swap_count_30d,
        "swaps_per_day":           round(swaps_per_day, 1),
        "distinct_tokens":         len(distinct_mints),
        "wallet_age_days":         wallet_age_days,
        "cross_token_appearances": cross_token_count,
        "bot_score":               bot_score,
    }


def composite_score(row: dict) -> float:
    """
    Higher = better candidate. Tuned to filter out sniper bots, which dominate
    trending-Solana-token early buyers. The sweet spot we reward:

      - Active but not extreme: ~1-5 swaps/day
      - Moderately diversified: 10-40 distinct tokens (NOT 60+; that's sniper churn)
      - Real history: 30+ days old
      - Moderate cross-token convergence: 2-5 of our trending tokens
        (8+ means they buy everything that pumps = bot)
    """
    score = 0.0

    # Activity (sweet spot: 1-5 swaps/day. >20/day is bot territory.)
    swaps_per_day = row.get("swaps_per_day", 0)
    if 1 <= swaps_per_day <= 5:
        score += 10
    elif 0.3 <= swaps_per_day < 1:
        score += 5
    elif 5 < swaps_per_day <= 10:
        score += 3
    elif swaps_per_day < 0.3:
        score -= 3
    else:  # > 10/day
        score -= (swaps_per_day - 10)

    # Diversity (sweet spot: 10-40 tokens; >60 is sniper churn)
    dt = row["distinct_tokens"]
    if 10 <= dt <= 40:
        score += 8
    elif 5 <= dt < 10:
        score += 3
    elif dt > 60:
        score -= 8
    elif dt > 40:
        score -= 3

    # Wallet age (older = more legit; capped at 1 year of bonus).
    # Soft penalty for very new wallets — fresh burn wallets are common in
    # Solana memecoin trading and aren't disqualifying on their own; the bot
    # detection already flags the suspicious churn patterns.
    score += min(row["wallet_age_days"] / 30, 12)
    if row["wallet_age_days"] < 7:
        score -= 3
    elif row["wallet_age_days"] < 30:
        score -= 1

    # Cross-token convergence (2-5 = good signal; 8+ = sniper that buys everything)
    xtok = row["cross_token_appearances"]
    if 2 <= xtok <= 5:
        score += xtok * 3
    elif 6 <= xtok <= 7:
        score += 2          # marginal — could be active human or active bot
    elif xtok >= 8:
        score -= (xtok - 7) * 5   # heavy penalty per excess appearance

    # Bot score (compounded penalty)
    score -= row["bot_score"] * 6

    return round(score, 2)


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )
    if not HELIUS_API_KEY:
        print("ERROR: HELIUS_API_KEY not set in .env - aborting.")
        return

    async with DexscreenerClient() as dex, \
               HeliusClient() as helius, \
               aiohttp.ClientSession() as rpc:

        # 1. Trending tokens
        logger.info("Fetching trending Solana tokens from Dexscreener...")
        tokens = await get_trending_tokens(dex)
        if not tokens:
            print("No trending tokens returned. Dexscreener may be down. Try again.")
            return
        logger.info("Got %d trending tokens to scan", len(tokens))

        # 2. Find buyers per token
        wallet_token_map: dict[str, set[str]] = defaultdict(set)
        for i, token in enumerate(tokens, 1):
            pair = await get_pair_address(dex, token)
            if not pair:
                logger.info("[%d/%d] %s: no Solana pair found, skipping", i, len(tokens), token[:8])
                await asyncio.sleep(0.4)
                continue
            buyers = await get_token_buyers(helius, pair, token)
            for w in buyers:
                wallet_token_map[w].add(token)
            logger.info("[%d/%d] %s: %d unique buyers", i, len(tokens), token[:8], len(buyers))
            await asyncio.sleep(0.6)  # ~1.6 req/s, under Helius free-tier 2 rps cap

        # 3. Cross-token filter
        candidates = [
            (w, toks) for w, toks in wallet_token_map.items()
            if len(toks) >= MIN_CROSS_TOKEN
        ]
        candidates.sort(key=lambda kv: -len(kv[1]))  # strongest convergence first
        candidates = candidates[:MAX_CANDIDATES]
        logger.info("Cross-token filter (>=%d): %d candidates to vet",
                    MIN_CROSS_TOKEN, len(candidates))

        # 4. Vet each candidate
        results = []
        for i, (wallet, tokens_set) in enumerate(candidates, 1):
            # Wallet age estimate #1: oldest signature via RPC paginate (covers all activity)
            oldest_sig_ts = await get_oldest_signature_ts(rpc, wallet, max_pages=5)
            age_from_sigs = (
                (datetime.now(timezone.utc) - oldest_sig_ts).days if oldest_sig_ts else 0
            )

            swaps = await helius.get_address_transactions(
                wallet, tx_type="SWAP", limit=SWAPS_PER_WALLET
            )

            # Wallet age estimate #2: oldest SWAP in the Enhanced-API window.
            # Take the max — whichever method saw further back is more accurate.
            oldest_swap_unix = None
            for tx in swaps:
                ts_u = tx.get("timestamp")
                if ts_u and (oldest_swap_unix is None or ts_u < oldest_swap_unix):
                    oldest_swap_unix = ts_u
            age_from_swaps = (
                (datetime.now(timezone.utc) -
                 datetime.fromtimestamp(oldest_swap_unix, tz=timezone.utc)).days
                if oldest_swap_unix else 0
            )
            wallet_age_days = max(age_from_sigs, age_from_swaps)
            metrics = vet_wallet(swaps, len(tokens_set), wallet_age_days)
            if not metrics:
                logger.info("[%d/%d] %s: no swap history, skipping",
                            i, len(candidates), wallet[:8])
                await asyncio.sleep(0.4)
                continue

            sol_balance = await get_sol_balance(rpc, wallet)
            row = {
                "address":      wallet,
                **metrics,
                "sol_balance":  round(sol_balance, 2),
                "size_bucket":  categorize_size(sol_balance),
            }
            row["composite_score"] = composite_score(row)
            results.append(row)
            logger.info(
                "[%d/%d] %s: score=%5.1f spd=%4.1f tokens=%d age=%4dd xtok=%2d bot=%d",
                i, len(candidates), wallet[:8],
                row["composite_score"], row["swaps_per_day"],
                row["distinct_tokens"], row["wallet_age_days"],
                row["cross_token_appearances"], row["bot_score"],
            )
            await asyncio.sleep(0.6)

        # 5. Write CSV
        if not results:
            print("\nNo candidates survived filtering. Try lowering MIN_CROSS_TOKEN to 1, "
                  "or run again later when more tokens are trending.")
            return

        results.sort(key=lambda r: r["composite_score"], reverse=True)
        out_path = Path(__file__).parent / "candidates.csv"
        fieldnames = [
            "composite_score", "address", "size_bucket", "sol_balance",
            "swap_count_30d", "swaps_per_day", "distinct_tokens", "wallet_age_days",
            "cross_token_appearances", "bot_score",
        ]
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for row in results:
                w.writerow({k: row.get(k) for k in fieldnames})

        print()
        print(f"Wrote {len(results)} candidates to {out_path}")
        print()
        print(f"{'Score':>6}  {'Address':<16}  {'Size':<18}  {'Spd':>5}  "
              f"{'Tokens':>6}  {'AgeD':>5}  {'XTok':>4}  {'Bot':>3}")
        print("-" * 82)
        for r in results[:15]:
            print(
                f"{r['composite_score']:>6.1f}  {r['address'][:16]:<16}  "
                f"{r['size_bucket']:<18}  {r['swaps_per_day']:>5.1f}  "
                f"{r['distinct_tokens']:>6}  {r['wallet_age_days']:>5}  "
                f"{r['cross_token_appearances']:>4}  {r['bot_score']:>3}"
            )
        print()
        # Quick interpretation hints
        good = [r for r in results if r["composite_score"] >= 20 and r["bot_score"] == 0]
        print(f"  {len(good)} candidates with score>=20 and bot_score=0 "
              f"(your shortlist).")
        bots = [r for r in results if r["bot_score"] >= 3]
        print(f"  {len(bots)} flagged as likely bots (bot_score>=3) — skip these.")
        print()
        print("Review candidates.csv. Add the ones with high score + bot_score=0 + age>30d")
        print("via the dashboard:  http://localhost:3001  ->  Wallets tab  ->  + Add Wallet")


if __name__ == "__main__":
    asyncio.run(main())
