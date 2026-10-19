"""Weekly research report: collection throughput and experiment outcomes.

Usage (needs only the database URL, e.g. from the Render shell or a laptop):

    python -m tradebot.verdict            # last 7 days of throughput
    python -m tradebot.verdict --days 14
    python -m tradebot.verdict --json

Outcome rules:
- Returns are Dexscreener market-price changes from the first qualifying
  observation. They are not executable returns: no fees, slippage or failed
  exits are modelled beyond the flat cost sensitivity lines.
- An unresolved check (no valid price inside its tolerance window) is never
  given a price. It appears in its own count and, separately, in a
  conservative scenario that values it at -100%.
- Each token counts once per cohort and horizon.
- Cost scenarios (2% and 5%) are one assumed deduction covering entry and
  exit together, applied identically to eligible and control returns. SOL is
  a zero-cost buy-and-hold benchmark. These are scenarios, not verified
  trading returns.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import statistics
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from tradebot import experiment
from tradebot.strategy import STRATEGY_VERSION

COST_SCENARIOS_PCT = (2, 5)
# A completed check observed within one scan cycle of its scheduled time is
# "on time"; later but still inside the horizon's tolerance is "late".
ON_TIME_WITHIN = timedelta(minutes=15)
STATUS_BUCKETS = ("not_yet_due", "waiting", "completed_on_time", "completed_late", "unresolved")
LOW_EXIT_LIQUIDITY_USD = Decimal(1000)

THROUGHPUT_SQL = """
SELECT date_trunc('day', started_at)::date AS day,
       count(*) FILTER (WHERE status = 'ok') AS cycles_ok,
       count(*) FILTER (WHERE status = 'error') AS cycles_error,
       count(*) FILTER (WHERE status = 'running') AS cycles_unfinished,
       array_agg(stats_json) FILTER (WHERE status IN ('ok','error')) AS stats
FROM scan_cycles
WHERE strategy_version = $1 AND started_at > now() - $2::interval
GROUP BY 1 ORDER BY 1
"""

OUTCOMES_SQL = """
SELECT oc.cohort, oc.check_kind, oc.token_address, oc.status, oc.resolution,
       oc.scheduled_at, oc.completed_at, oc.market_price_change_pct,
       oc.outcome_liquidity_usd, oc.sol_baseline_price_usd, oc.sol_outcome_price_usd,
       COALESCE(oc.trigger_source, 'unknown') AS trigger_source,
       ms.captured_at AS baseline_at
FROM outcome_checks oc
JOIN strategy_versions sv ON sv.id = oc.strategy_version_id
LEFT JOIN market_snapshots ms ON ms.id = oc.baseline_market_snapshot_id
WHERE sv.version = $1 AND oc.cohort IS NOT NULL
"""

COUNTER_KEYS = (
    "profile_tokens",
    "recheck_tokens",
    "recheck_pairs_found",
    "provider_errors",
    "scored",
    "passed_hard_filters",
    "eligible",
    "eligible_tokens_started",
    "control_tokens_started",
    "outcomes_completed",
    "outcomes_retried",
    "outcomes_unresolved",
)


def _stats(raw: Any) -> dict[str, Any]:
    return json.loads(raw) if isinstance(raw, str) else (raw or {})


def summarize_throughput(rows: list[Any]) -> dict[str, Any]:
    days = []
    reasons: dict[str, int] = defaultdict(int)
    missing: dict[str, int] = defaultdict(int)
    pool_sizes: list[int] = []
    totals: dict[str, int] = defaultdict(int)
    for row in rows:
        day = {
            "day": str(row["day"]),
            "cycles_ok": row["cycles_ok"],
            "cycles_error": row["cycles_error"],
            "cycles_unfinished": row["cycles_unfinished"],
        }
        for raw in row["stats"] or []:
            stats = _stats(raw)
            for key in COUNTER_KEYS:
                day[key] = day.get(key, 0) + int(stats.get(key, 0))
            for reason, count in (stats.get("rejection_reasons") or {}).items():
                reasons[reason] += int(count)
            for reason, count in (stats.get("missing_inputs") or {}).items():
                missing[reason] += int(count)
            if stats.get("recheck_universe"):
                pool_sizes.append(int(stats["recheck_universe"]))
        for key in ("cycles_ok", "cycles_error", "cycles_unfinished", *COUNTER_KEYS):
            totals[key] += int(day.get(key, 0))
        days.append(day)
    return {
        "days": days,
        "totals": dict(totals),
        # Before the 0004 patch, missing-input rejections were counted with rule
        # failures; only cycles after it separate them.
        "rejection_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "missing_inputs": dict(sorted(missing.items(), key=lambda kv: -kv[1])),
        # Size of the 1-7 day recheck pool before the 300 cap, per cycle.
        "recheck_pool": {
            "cycles": len(pool_sizes),
            "latest": pool_sizes[-1] if pool_sizes else None,
            "min": min(pool_sizes) if pool_sizes else None,
            "max": max(pool_sizes) if pool_sizes else None,
        },
    }


def _pct(value: Any) -> float | None:
    return None if value is None else float(value)


def _describe(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    out = {
        "n": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "pct_positive": 100 * sum(v > 0 for v in values) / len(values),
        "pct_losing": 100 * sum(v < 0 for v in values) / len(values),
        "worst": ordered[:3],
        "best": ordered[-3:][::-1],
    }
    for drop in (1, 2):
        rest = ordered[:-drop]
        out[f"mean_without_top_{drop}"] = statistics.fmean(rest) if rest else None
    gains = sum(v for v in values if v > 0)
    out["top_winner_share_of_gains"] = (ordered[-1] / gains) if gains > 0 else None
    return out


def _status(row: Any, now: datetime) -> str:
    """Exactly one bucket per check, so the buckets always sum to tokens."""
    if row["status"] == "completed" and row["market_price_change_pct"] is not None:
        late = row["completed_at"] - row["scheduled_at"] > ON_TIME_WITHIN
        return "completed_late" if late else "completed_on_time"
    if row["status"] in {"unresolved", "failed", "completed"}:
        return "unresolved"
    return "not_yet_due" if row["scheduled_at"] > now else "waiting"


def cohort_overlap(rows: list[Any]) -> dict[str, Any]:
    """Tokens measured in both cohorts. A control token that later qualified
    keeps its control record; its eligible record starts at the later price."""
    cohorts: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        cohorts[row["token_address"]].add(row["cohort"])
    both = sorted(t for t, c in cohorts.items() if {"eligible", "control"} <= c)
    return {"tokens_in_both": len(both), "tokens": both}


def enrolment(rows: list[Any]) -> dict[str, dict[str, int]]:
    """Unique tokens enrolled per cohort, by what triggered the enrolment."""
    seen: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for row in rows:
        seen[row["cohort"]][row["trigger_source"]].add(row["token_address"])
    return {
        cohort: {"total": len(set().union(*by_source.values())), **{s: len(t) for s, t in sorted(by_source.items())}}
        for cohort, by_source in sorted(seen.items())
    }


def summarize_outcomes(rows: list[Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        group = groups.setdefault(
            (row["cohort"], row["check_kind"]),
            {
                "tokens": {},
                "observed": [],
                "unresolved": defaultdict(int),
                "status": defaultdict(int),
                "tokens_by_source": defaultdict(int),
            },
        )
        # One record per token per cohort/horizon is enforced by the
        # idempotency key; this guards the report against any duplicate.
        if row["token_address"] in group["tokens"]:
            continue
        group["tokens"][row["token_address"]] = row
        group["tokens_by_source"][row["trigger_source"]] += 1
        group["status"][_status(row, now)] += 1
        if row["status"] == "completed" and row["market_price_change_pct"] is not None:
            group["observed"].append(row)
        elif row["status"] in {"unresolved", "failed"}:
            group["unresolved"][row["resolution"] or row["status"]] += 1

    report: dict[str, Any] = {}
    for (cohort, kind), group in sorted(groups.items()):
        observed = group["observed"]
        returns = [_pct(r["market_price_change_pct"]) for r in observed]
        unresolved_n = sum(group["unresolved"].values())
        excess, sol_returns = [], []
        for r in observed:
            base, end = r["sol_baseline_price_usd"], r["sol_outcome_price_usd"]
            if base and end and base > 0:
                sol = float((end - base) / base * 100)
                sol_returns.append(sol)
                excess.append(_pct(r["market_price_change_pct"]) - sol)
        by_day: dict[str, list[float]] = defaultdict(list)
        dated = []
        for r in observed:
            if r["baseline_at"] is not None:
                by_day[str(r["baseline_at"].date())].append(_pct(r["market_price_change_pct"]))
                dated.append((r["baseline_at"], _pct(r["market_price_change_pct"])))
        dated.sort(key=lambda item: item[0])
        half = len(dated) // 2
        report[f"{cohort}/{kind}"] = {
            "primary": kind == experiment.PRIMARY_HORIZON,
            "tokens": len(group["tokens"]),
            "observed": _describe(returns),
            "unresolved": dict(group["unresolved"]),
            "status": {k: group["status"].get(k, 0) for k in STATUS_BUCKETS},
            "pending": group["status"].get("not_yet_due", 0) + group["status"].get("waiting", 0),
            "tokens_by_source": dict(group["tokens_by_source"]),
            "observed_by_source": {
                source: _describe([_pct(r["market_price_change_pct"]) for r in observed if r["trigger_source"] == source])
                for source in sorted({r["trigger_source"] for r in observed})
            },
            "conservative_unresolved_as_minus_100": _describe(
                returns + [-100.0] * unresolved_n
            ),
            "low_exit_liquidity": sum(
                1
                for r in observed
                if r["outcome_liquidity_usd"] is not None
                and r["outcome_liquidity_usd"] < LOW_EXIT_LIQUIDITY_USD
            ),
            "sol_same_window": _describe(sol_returns),
            "excess_vs_sol": _describe(excess),
            "after_costs": {
                f"{cost}pct_round_trip": _describe([v - cost for v in returns])
                for cost in COST_SCENARIOS_PCT
            },
            # Token return after the assumed cost minus SOL's zero-cost return.
            "excess_vs_sol_after_costs": {
                f"{cost}pct_round_trip": _describe([v - cost for v in excess])
                for cost in COST_SCENARIOS_PCT
            },
            "halves": [
                _describe([v for _, v in dated[:half]]),
                _describe([v for _, v in dated[half:]]),
            ],
            "median_by_discovery_day": {
                day: {"n": len(v), "median": statistics.median(v)}
                for day, v in sorted(by_day.items())
            },
        }
    return report


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:+.1f}"
    if isinstance(value, list):
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    return str(value)


COST_NOTE = (
    "Cost scenarios: one assumed 2% or 5% deduction covering entry and exit together, "
    "applied the same way to eligible and control returns. SOL is a zero-cost "
    "buy-and-hold benchmark. Scenarios, not verified trading returns."
)
POOL_NOTE = (
    "Counts are observations and include repeats. Failed rechecks are counted, not stored, "
    "so unique tokens assessed and per-source qualification rates are not available."
)


def render_text(report: dict[str, Any]) -> str:
    throughput, outcomes = report["throughput"], report["outcomes"]
    status = report.get("status", {})
    lines = [f"Tradebot research report: {STRATEGY_VERSION}", "", "1. IS IT WORKING?"]
    lines.append(f"  last successful scan: {status.get('last_ok_at')}   scans in 24h: "
                 f"{status.get('ok_24h')} ok, {status.get('errors_24h')} failed   "
                 f"overdue checks: {status.get('overdue_checks')}")
    lines += [f"  ATTENTION: {p}" for p in report.get("attention", [])] or ["  Nothing needs attention."]

    lines += ["", "2. IS IT FINDING SUITABLE TOKENS?", f"  {POOL_NOTE}"]
    header = ("day", "ok", "err", "scored", "hard_pass", "eligible", "recheck", "prov_err")
    lines.append("  " + "  ".join(f"{h:>10}" for h in header))
    for d in throughput["days"]:
        cells = (d["day"], d["cycles_ok"], d["cycles_error"], d.get("scored", 0),
                 d.get("passed_hard_filters", 0), d.get("eligible", 0),
                 d.get("recheck_tokens", 0), d.get("provider_errors", 0))
        lines.append("  " + "  ".join(f"{c!s:>10}" for c in cells))
    pool = throughput["recheck_pool"]
    lines.append(f"  recheck pool (1-7 day tokens, before the 300 cap): latest {pool['latest']}, "
                 f"range {pool['min']}-{pool['max']} over {pool['cycles']} cycles")
    lines.append("  unique tokens enrolled (exact): " + ("  ".join(
        f"{cohort}: {counts['total']} ({', '.join(f'{k} {v}' for k, v in counts.items() if k != 'total')})"
        for cohort, counts in report.get("enrolment", {}).items()) or "none yet"))
    lines.append(f"  tokens in both cohorts (control first, later eligible): {report.get('overlap', {}).get('tokens_in_both', 0)}")
    lines.append("  rule failures (input present):")
    lines += [f"    {n:>7}  {r}" for r, n in throughput["rejection_reasons"].items()] or ["    none"]
    lines.append("  rejected because an input was missing or invalid:")
    lines += [f"    {n:>7}  {r}" for r, n in throughput["missing_inputs"].items()] or ["    none"]

    lines += ["", "3. WHAT HAPPENED AFTERWARDS? (market-price % change; not executable returns)", f"  {COST_NOTE}"]
    if not outcomes:
        lines.append("  No experiment outcomes recorded yet.")
    for key, item in outcomes.items():
        o, c, st = item["observed"], item["conservative_unresolved_as_minus_100"], item["status"]
        tag = "  <- primary" if item["primary"] else ""
        lines += ["", f"{key}{tag}",
                  (f"  tokens {item['tokens']}: not yet due {st['not_yet_due']}, waiting {st['waiting']}, "
                  f"on time {st['completed_on_time']}, late {st['completed_late']}, unresolved {st['unresolved']} "
                  f"{item['unresolved'] or ''}"),
                  "  by trigger: " + ", ".join(f"{k} {v}" for k, v in sorted(item["tokens_by_source"].items()))]
        if o["n"]:
            lines += [
                f"  median {_fmt(o['median'])}  mean {_fmt(o['mean'])}  positive {o['pct_positive']:.0f}%  losing {o['pct_losing']:.0f}%  worst {_fmt(o['worst'])}  best {_fmt(o['best'])}",
                f"  mean w/o top1 {_fmt(o['mean_without_top_1'])}  w/o top2 {_fmt(o['mean_without_top_2'])}",
                f"  conservative (unresolved = -100%, an assumption): n {c['n']}  median {_fmt(c['median'])}  mean {_fmt(c['mean'])}",
                "  after assumed costs: " + "  ".join(
                    f"{name} median {_fmt(v.get('median'))} mean {_fmt(v.get('mean'))}" for name, v in item["after_costs"].items()),
                (f"  SOL (zero-cost buy-and-hold) same windows: median {_fmt(item['sol_same_window'].get('median'))}   "
                f"excess vs SOL: median {_fmt(item['excess_vs_sol'].get('median'))}, after 2% {_fmt(item['excess_vs_sol_after_costs']['2pct_round_trip'].get('median'))}"),
                "  by trigger (overlapping populations, not a causal comparison): " + "  ".join(
                    f"{k} n {v['n']} median {_fmt(v.get('median'))}" for k, v in item["observed_by_source"].items()),
                "  halves by discovery time: " + "  |  ".join(
                    f"n {h['n']} median {_fmt(h.get('median'))}" for h in item["halves"]),
            ]
    return "\n".join(lines)


SCAN_STATUS_SQL = """
SELECT
    (SELECT max(finished_at) FROM scan_cycles WHERE status = 'ok') AS last_ok_at,
    (SELECT row_to_json(c) FROM (
        SELECT status, error, started_at, finished_at FROM scan_cycles
        ORDER BY started_at DESC LIMIT 1) c) AS last_cycle,
    (SELECT count(*) FROM scan_cycles
     WHERE status = 'ok' AND started_at > now() - interval '24 hours') AS ok_24h,
    (SELECT count(*) FROM scan_cycles
     WHERE status = 'error' AND started_at > now() - interval '24 hours') AS errors_24h,
    (SELECT count(*) FROM outcome_checks
     WHERE status IN ('pending','retryable') AND due_at < now() - interval '30 minutes') AS overdue_checks
"""

# A healthy scanner finishes a cycle every 15 minutes; three missed cycles is a problem.
STALE_SCAN_AFTER = timedelta(minutes=45)


def scan_attention(status: dict[str, Any], now: datetime | None = None) -> list[str]:
    now = now or datetime.now(UTC)
    problems = []
    last_ok = status.get("last_ok_at")
    if last_ok is None:
        problems.append("No successful scan recorded yet.")
    elif now - last_ok > STALE_SCAN_AFTER:
        minutes = int((now - last_ok).total_seconds() // 60)
        problems.append(f"Last successful scan was {minutes} minutes ago.")
    last = status.get("last_cycle") or {}
    if last.get("status") == "error":
        problems.append(f"Most recent scan failed: {last.get('error') or 'unknown error'}.")
    if status.get("errors_24h"):
        problems.append(f"{status['errors_24h']} failed scans in the last 24 hours.")
    if status.get("overdue_checks"):
        problems.append(f"{status['overdue_checks']} price checks are more than 30 minutes overdue.")
    return problems


async def collect_from_conn(conn: Any, days: int) -> dict[str, Any]:
    throughput_rows = await conn.fetch(THROUGHPUT_SQL, STRATEGY_VERSION, timedelta(days=days))
    outcome_rows = list(await conn.fetch(OUTCOMES_SQL, STRATEGY_VERSION))
    status_row = await conn.fetchrow(SCAN_STATUS_SQL)
    status = dict(status_row) if status_row else {}
    if isinstance(status.get("last_cycle"), str):
        status["last_cycle"] = json.loads(status["last_cycle"])
    return {
        "status": status,
        "attention": scan_attention(status),
        "throughput": summarize_throughput(list(throughput_rows)),
        "outcomes": summarize_outcomes(outcome_rows),
        "enrolment": enrolment(outcome_rows),
        "overlap": cohort_overlap(outcome_rows),
        "observations": outcome_rows,
    }


async def collect(database_url: str, days: int) -> dict[str, Any]:
    import asyncpg

    # asyncpg wants a plain libpq URL; Alembic-style driver suffixes are dropped.
    conn = await asyncpg.connect(dsn=database_url.replace("postgresql+psycopg://", "postgresql://", 1))
    try:
        return await collect_from_conn(conn, days)
    finally:
        await conn.close()


SUMMARY_COLUMNS = (
    "cohort", "horizon", "primary", "tokens", "observed", "unresolved", "pending",
    "not_yet_due", "waiting", "completed_on_time", "completed_late",
    "tokens_profile_feed", "tokens_recheck", "tokens_unknown_source",
    "mean_pct", "median_pct", "pct_positive", "pct_losing", "worst_pct", "best_pct",
    "mean_without_top_1", "mean_without_top_2", "top_winner_share_of_gains",
    "conservative_mean_pct", "conservative_median_pct",
    "sol_median_pct", "excess_vs_sol_mean_pct", "excess_vs_sol_median_pct",
    "after_2pct_mean", "after_2pct_median", "after_5pct_mean", "after_5pct_median",
    "excess_vs_sol_after_2pct_median", "excess_vs_sol_after_5pct_median",
    "first_half_n", "first_half_median", "second_half_n", "second_half_median",
    "exits_below_1000_liquidity",
)


def summary_rows(outcomes: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key, item in outcomes.items():
        cohort, horizon = key.split("/", 1)
        o = item["observed"]
        c = item["conservative_unresolved_as_minus_100"]
        costs = item["after_costs"]
        first, second = item["halves"]
        rows.append({
            "cohort": cohort,
            "horizon": horizon,
            "primary": item["primary"],
            "tokens": item["tokens"],
            "observed": o["n"],
            "unresolved": sum(item["unresolved"].values()),
            "pending": item["pending"],
            **item["status"],
            "tokens_profile_feed": item["tokens_by_source"].get("profile_feed", 0),
            "tokens_recheck": item["tokens_by_source"].get("recheck", 0),
            "tokens_unknown_source": item["tokens_by_source"].get("unknown", 0),
            "mean_pct": o.get("mean"),
            "median_pct": o.get("median"),
            "pct_positive": o.get("pct_positive"),
            "pct_losing": o.get("pct_losing"),
            "worst_pct": (o.get("worst") or [None])[0],
            "best_pct": (o.get("best") or [None])[0],
            "mean_without_top_1": o.get("mean_without_top_1"),
            "mean_without_top_2": o.get("mean_without_top_2"),
            "top_winner_share_of_gains": o.get("top_winner_share_of_gains"),
            "conservative_mean_pct": c.get("mean"),
            "conservative_median_pct": c.get("median"),
            "sol_median_pct": item["sol_same_window"].get("median"),
            "excess_vs_sol_mean_pct": item["excess_vs_sol"].get("mean"),
            "excess_vs_sol_median_pct": item["excess_vs_sol"].get("median"),
            "after_2pct_mean": costs["2pct_round_trip"].get("mean"),
            "after_2pct_median": costs["2pct_round_trip"].get("median"),
            "after_5pct_mean": costs["5pct_round_trip"].get("mean"),
            "after_5pct_median": costs["5pct_round_trip"].get("median"),
            "excess_vs_sol_after_2pct_median": item["excess_vs_sol_after_costs"]["2pct_round_trip"].get("median"),
            "excess_vs_sol_after_5pct_median": item["excess_vs_sol_after_costs"]["5pct_round_trip"].get("median"),
            "first_half_n": first["n"],
            "first_half_median": first.get("median"),
            "second_half_n": second["n"],
            "second_half_median": second.get("median"),
            "exits_below_1000_liquidity": item["low_exit_liquidity"],
        })
    return rows


OBSERVATION_COLUMNS = (
    "cohort", "check_kind", "token_address", "trigger_source", "status", "resolution", "baseline_at",
    "scheduled_at", "completed_at", "market_price_change_pct", "outcome_liquidity_usd",
    "sol_baseline_price_usd", "sol_outcome_price_usd",
)


def to_csv(rows: list[Any], columns: tuple[str, ...]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if row[k] is None else row[k]) for k in columns})
    return buffer.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        parser.error("DATABASE_URL is required")
    report = asyncio.run(collect(database_url, args.days))
    if args.json:
        report.pop("observations")
        print(json.dumps(report, default=str, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
