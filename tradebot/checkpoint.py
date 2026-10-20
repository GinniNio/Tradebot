"""Read-only checks for the 2026-10-26 v2 checkpoint and the v3 decision.

    python -m tradebot.checkpoint [--days 7] [--graduates] [--json]

Needs only DATABASE_URL, and runs in a READ ONLY transaction. It complements
`python -m tradebot.verdict` (scan health, throughput, outcomes) with three
things the report does not show, and it never reads an outcome return:

1. Score bands of stored hard-filter passers, recomputed with the frozen
   scorer. Failed rechecks are not stored, so these are observations of
   discovery rows and of rechecks that passed, with repeats.
2. Control tokens' exact score at enrolment, recomputed from the snapshot each
   enrolment points to, next to the stored candidate score (which is the
   first observation's).
3. With --graduates: the tokens first seen 7 or more days ago, looked up once
   on Dexscreener now, to size a v3 universe built from the bot's own
   graduates. This is a single snapshot in time, not a weekly rate, and
   applies the proposed v3 Option A pool aggregation (not v2's single pool).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from tradebot.strategy import STRATEGY_VERSION, evaluate

BANDS = ("<50", "50-64", "65+")
SCORE_FIELDS = (
    "liquidity_usd", "volume_1h_usd", "volume_6h_usd", "volume_24h_usd",
    "price_change_1h_pct", "price_change_24h_pct", "market_cap_usd", "fdv_usd",
)
# Proposed v3 inputs, used only by --graduates: pools under this liquidity are
# ignored when summing (Option A in docs/V3_SPEC_DRAFT.md).
DUST_POOL_USD = Decimal(10000)
AGE_BUCKETS = ((7, 14), (14, 30), (30, None))

PASSER_SQL = """
SELECT rc.token_address, ms.captured_at,
       ms.liquidity_usd, ms.volume_1h_usd, ms.volume_6h_usd, ms.volume_24h_usd,
       ms.price_change_1h_pct, ms.price_change_24h_pct, ms.market_cap_usd, ms.fdv_usd
FROM market_snapshots ms
JOIN research_candidates rc ON rc.id = ms.candidate_id
JOIN strategy_versions sv ON sv.id = rc.strategy_version_id
WHERE sv.version = $1 AND ms.captured_at >= now() - make_interval(days => $2)
ORDER BY ms.captured_at
"""

CONTROL_SQL = """
SELECT oc.token_address, rc.score AS stored_score,
       ms.liquidity_usd, ms.volume_1h_usd, ms.volume_6h_usd, ms.volume_24h_usd,
       ms.price_change_1h_pct, ms.price_change_24h_pct, ms.market_cap_usd, ms.fdv_usd
FROM outcome_checks oc
JOIN strategy_versions sv ON sv.id = oc.strategy_version_id
JOIN research_candidates rc ON rc.id = oc.candidate_id
LEFT JOIN market_snapshots ms ON ms.id = oc.baseline_market_snapshot_id
WHERE sv.version = $1 AND oc.cohort = 'control' AND oc.check_kind = '24h'
"""

GRADUATE_SQL = """
SELECT token_address, min(created_at) AS first_seen
FROM research_candidates
GROUP BY token_address
HAVING min(created_at) < now() - interval '7 days'
"""


def band(score: Decimal | float) -> str:
    return "65+" if score >= 65 else "50-64" if score >= 50 else "<50"


def _score(row: Any) -> dict[str, Any]:
    return evaluate({k: row[k] for k in SCORE_FIELDS})


def summarize_passers(rows: list[Any]) -> dict[str, Any]:
    """Band counts of hard-filter passers, and how each token first reached 50+.

    `observed_crossing` means an earlier stored hard-filter pass of the same
    token scored under 50; `first_seen_above` means its first stored pass was
    already 50+. Rows must be in capture order.
    """
    observations: Counter[str] = Counter()
    best: dict[str, str] = {}
    below_first: set[str] = set()
    entry: dict[str, str] = {}
    for row in rows:
        result = _score(row)
        if not result["passed_hard_filters"]:
            continue
        token, b = row["token_address"], band(result["score"])
        observations[b] += 1
        if token not in best or BANDS.index(b) > BANDS.index(best[token]):
            best[token] = b
        if b == "<50":
            if token not in entry:
                below_first.add(token)
        elif token not in entry:
            entry[token] = "observed_crossing" if token in below_first else "first_seen_above"
    return {
        "observations_by_band": {b: observations[b] for b in BANDS},
        "tokens_by_best_band": {b: sum(v == b for v in best.values()) for b in BANDS},
        "tokens_reaching_50_by_entry": dict(Counter(entry.values())),
    }


def summarize_control(rows: list[Any]) -> dict[str, Any]:
    at_entry: Counter[str] = Counter()
    differs = missing = 0
    for row in rows:
        if row["liquidity_usd"] is None and row["price_change_24h_pct"] is None:
            missing += 1
            continue
        score = _score(row)["score"]
        at_entry[band(score)] += 1
        if row["stored_score"] is not None and Decimal(row["stored_score"]) != score:
            differs += 1
    return {
        "tokens": len(rows),
        "score_at_entry_by_band": {b: at_entry[b] for b in BANDS},
        "stored_score_differs_from_entry": differs,
        "snapshot_missing": missing,
    }


def _d(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (ArithmeticError, ValueError, TypeError):
        return None


def option_a_snapshot(pairs: list[dict[str, Any]], token: str) -> dict[str, Any] | None:
    """Proposed v3 inputs: liquidity and volumes summed over the token's pools of
    at least DUST_POOL_USD; price change, market cap and pool age from the deepest."""
    own = [
        p for p in pairs
        if (p.get("baseToken") or {}).get("address") == token
        and (_d((p.get("liquidity") or {}).get("usd")) or 0) >= DUST_POOL_USD
    ]
    if not own:
        return None
    deepest = max(own, key=lambda p: _d(p["liquidity"]["usd"]))

    def total(*path: str) -> Decimal:
        out = Decimal(0)
        for p in own:
            value: Any = p
            for key in path:
                value = value.get(key) if isinstance(value, dict) else None
            out += _d(value) or 0
        return out

    created = deepest.get("pairCreatedAt")
    return {
        "deepest_liquidity_usd": _d(deepest["liquidity"]["usd"]),
        "pool_age_days": None if not created else
        (datetime.now(UTC).timestamp() - int(created) / 1000) / 86400,
        "liquidity_usd": total("liquidity", "usd"),
        "volume_1h_usd": total("volume", "h1"),
        "volume_6h_usd": total("volume", "h6"),
        "volume_24h_usd": total("volume", "h24"),
        "price_change_1h_pct": (deepest.get("priceChange") or {}).get("h1"),
        "price_change_24h_pct": (deepest.get("priceChange") or {}).get("h24"),
        "market_cap_usd": deepest.get("marketCap"),
        "fdv_usd": deepest.get("fdv"),
    }


def summarize_graduates(first_seen: dict[str, datetime], pairs_by_token: dict[str, list]) -> dict[str, Any]:
    now = datetime.now(UTC)
    out: dict[str, Any] = {}
    for low, high in AGE_BUCKETS:
        label = f"{low}-{high}d" if high else f"{low}d+"
        counts: Counter[str] = Counter()
        for token, seen in first_seen.items():
            age = (now - seen).days
            if age < low or (high is not None and age >= high):
                continue
            counts["first_seen"] += 1
            snap = option_a_snapshot(pairs_by_token.get(token, []), token)
            if snap is None:
                continue
            counts["listed_pool_10k_plus"] += 1
            if snap["deepest_liquidity_usd"] >= 50000:
                counts["deepest_pool_50k_plus"] += 1
            if snap["pool_age_days"] is not None and snap["pool_age_days"] >= 7:
                counts["deepest_pool_7d_plus"] += 1
            result = evaluate(snap)
            if result["passed_hard_filters"] and snap["deepest_liquidity_usd"] >= 50000:
                counts["passes_hard_filters_now"] += 1
                counts[f"now_{band(result['score'])}"] += 1
        out[label] = dict(counts)
    return out


async def fetch_pairs(tokens: list[str]) -> dict[str, list]:
    """All pools per token. The 30-token batch endpoint returns one pool per
    token, so it is only a prefilter: tokens whose returned pool is under
    DUST_POOL_USD (or that return nothing) are not looked up pool by pool."""
    from tradebot.config import ProviderBudget
    from tradebot.dexscreener import DexscreenerClient, ProviderError

    client = DexscreenerClient(ProviderBudget(2, 20.0, 2, 60))
    alive: list[str] = []
    for i in range(0, len(tokens), 30):
        chunk = tokens[i:i + 30]
        try:
            payload = await client.get_json(f"/tokens/v1/solana/{','.join(chunk)}")
        except ProviderError:
            continue
        for pair in payload if isinstance(payload, list) else []:
            address = (pair.get("baseToken") or {}).get("address")
            liquidity = _d((pair.get("liquidity") or {}).get("usd")) or 0
            if address in chunk and liquidity >= DUST_POOL_USD and address not in alive:
                alive.append(address)
    found: dict[str, list] = {}
    for token in alive:
        try:
            payload = await client.get_json(f"/token-pairs/v1/solana/{token}")
        except ProviderError:
            continue
        found[token] = payload if isinstance(payload, list) else []
    return found


async def collect(database_url: str, days: int, graduates: bool) -> dict[str, Any]:
    import asyncpg

    conn = await asyncpg.connect(dsn=database_url.replace("postgresql+psycopg://", "postgresql://", 1))
    try:
        async with conn.transaction(readonly=True):
            passers = await conn.fetch(PASSER_SQL, STRATEGY_VERSION, days)
            control = await conn.fetch(CONTROL_SQL, STRATEGY_VERSION)
            grads = await conn.fetch(GRADUATE_SQL) if graduates else []
    finally:
        await conn.close()
    report: dict[str, Any] = {
        "strategy_version": STRATEGY_VERSION,
        "days": days,
        "stored_hard_filter_passers": summarize_passers(list(passers)),
        "control_enrolment": summarize_control(list(control)),
    }
    if graduates:
        first_seen = {r["token_address"]: r["first_seen"] for r in grads}
        report["graduates"] = summarize_graduates(first_seen, await fetch_pairs(list(first_seen)))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--graduates", action="store_true", help="also look up tokens first seen 7+ days ago on Dexscreener")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        parser.error("DATABASE_URL is required")
    report = asyncio.run(collect(database_url, args.days, args.graduates))
    if args.json:
        print(json.dumps(report, default=str, indent=2))
    else:
        for section, body in report.items():
            print(f"{section}: {json.dumps(body, default=str)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
