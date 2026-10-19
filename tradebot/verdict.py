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
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from tradebot import experiment
from tradebot.strategy import STRATEGY_VERSION

COST_SCENARIOS_PCT = (2, 5)
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
        for key in ("cycles_ok", "cycles_error", "cycles_unfinished", *COUNTER_KEYS):
            totals[key] += int(day.get(key, 0))
        days.append(day)
    return {
        "days": days,
        "totals": dict(totals),
        "rejection_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
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


def summarize_outcomes(rows: list[Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        group = groups.setdefault(
            (row["cohort"], row["check_kind"]),
            {"tokens": {}, "observed": [], "unresolved": defaultdict(int), "pending": 0},
        )
        # One record per token per cohort/horizon is enforced by the
        # idempotency key; this guards the report against any duplicate.
        if row["token_address"] in group["tokens"]:
            continue
        group["tokens"][row["token_address"]] = row
        if row["status"] == "completed" and row["market_price_change_pct"] is not None:
            group["observed"].append(row)
        elif row["status"] in {"unresolved", "failed"}:
            group["unresolved"][row["resolution"] or row["status"]] += 1
        else:
            group["pending"] += 1

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
            "pending": group["pending"],
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


def render_text(throughput: dict[str, Any], outcomes: dict[str, Any]) -> str:
    lines = [f"Tradebot research report: {STRATEGY_VERSION}", "", "THROUGHPUT"]
    header = ("day", "ok", "err", "scored", "hard_pass", "eligible", "elig_start", "ctrl_start", "recheck", "prov_err")
    lines.append("  ".join(f"{h:>10}" for h in header))
    for d in throughput["days"]:
        cells = (
            d["day"], d["cycles_ok"], d["cycles_error"], d.get("scored", 0),
            d.get("passed_hard_filters", 0), d.get("eligible", 0),
            d.get("eligible_tokens_started", 0), d.get("control_tokens_started", 0),
            d.get("recheck_tokens", 0), d.get("provider_errors", 0),
        )
        lines.append("  ".join(f"{c!s:>10}" for c in cells))
    lines += ["", "Rejection reasons (count of observations; one token can fail several):"]
    for reason, count in throughput["rejection_reasons"].items():
        lines.append(f"  {count:>7}  {reason}")
    lines += ["", "OUTCOMES (market-price % change; not executable returns)"]
    if not outcomes:
        lines.append("  No experiment outcomes recorded yet.")
    for key, item in outcomes.items():
        o, c = item["observed"], item["conservative_unresolved_as_minus_100"]
        tag = "  <- primary" if item["primary"] else ""
        lines += [
            "",
            f"{key}{tag}",
            f"  tokens {item['tokens']}  observed {o['n']}  unresolved {sum(item['unresolved'].values())} {item['unresolved'] or ''}  pending {item['pending']}",
        ]
        if o["n"]:
            lines += [
                f"  mean {_fmt(o['mean'])}  median {_fmt(o['median'])}  losing {o['pct_losing']:.0f}%  worst {_fmt(o['worst'])}  best {_fmt(o['best'])}",
                f"  mean w/o top1 {_fmt(o['mean_without_top_1'])}  w/o top2 {_fmt(o['mean_without_top_2'])}  top winner share of gains {'-' if o['top_winner_share_of_gains'] is None else f"{100 * o['top_winner_share_of_gains']:.0f}%"}",
                f"  conservative (unresolved = -100%): n {c['n']}  mean {_fmt(c['mean'])}  median {_fmt(c['median'])}",
                f"  SOL same windows: n {item['sol_same_window']['n']}  median {_fmt(item['sol_same_window'].get('median'))}   excess vs SOL: mean {_fmt(item['excess_vs_sol'].get('mean'))}  median {_fmt(item['excess_vs_sol'].get('median'))}",
                "  after costs: " + "  ".join(
                    f"{name} mean {_fmt(v.get('mean'))} median {_fmt(v.get('median'))}"
                    for name, v in item["after_costs"].items()
                ),
                "  halves by discovery time: " + "  |  ".join(
                    f"n {h['n']} mean {_fmt(h.get('mean'))} median {_fmt(h.get('median'))}"
                    for h in item["halves"]
                ),
                f"  exits below ${LOW_EXIT_LIQUIDITY_USD} liquidity: {item['low_exit_liquidity']}",
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
    now = now or datetime.now(timezone.utc)
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
    "mean_pct", "median_pct", "pct_losing", "worst_pct", "best_pct",
    "mean_without_top_1", "mean_without_top_2", "top_winner_share_of_gains",
    "conservative_mean_pct", "conservative_median_pct",
    "sol_median_pct", "excess_vs_sol_mean_pct", "excess_vs_sol_median_pct",
    "after_2pct_mean", "after_2pct_median", "after_5pct_mean", "after_5pct_median",
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
            "mean_pct": o.get("mean"),
            "median_pct": o.get("median"),
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
            "first_half_n": first["n"],
            "first_half_median": first.get("median"),
            "second_half_n": second["n"],
            "second_half_median": second.get("median"),
            "exits_below_1000_liquidity": item["low_exit_liquidity"],
        })
    return rows


OBSERVATION_COLUMNS = (
    "cohort", "check_kind", "token_address", "status", "resolution", "baseline_at",
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
        for problem in report["attention"]:
            print(f"ATTENTION: {problem}")
        print(render_text(report["throughput"], report["outcomes"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
