from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradebot.verdict import (
    STATUS_BUCKETS,
    SUMMARY_COLUMNS,
    cohort_overlap,
    enrolment,
    render_text,
    scan_attention,
    summarize_outcomes,
    summarize_throughput,
    summary_rows,
    to_csv,
)

T0 = datetime(2026, 10, 20, tzinfo=UTC)


def row(token, change=None, status="completed", resolution="observed", kind="24h", cohort="eligible", day=0,
        source="profile_feed", completed_after=timedelta(0), scheduled=T0):
    return {
        "trigger_source": source,
        "cohort": cohort,
        "check_kind": kind,
        "token_address": token,
        "status": status,
        "resolution": resolution,
        "scheduled_at": scheduled,
        "completed_at": scheduled + completed_after,
        "market_price_change_pct": None if change is None else Decimal(str(change)),
        "outcome_liquidity_usd": Decimal(100000),
        "sol_baseline_price_usd": Decimal(100),
        "sol_outcome_price_usd": Decimal(102),
        "baseline_at": T0 + timedelta(days=day),
    }


def test_mean_and_median_disagree_and_both_are_reported():
    # Nine +1% and one -50%: median +1%, mean -4.1%. A median-only rule would pass this.
    rows = [row(f"t{i}", 1) for i in range(9)] + [row("loser", -50)]
    observed = summarize_outcomes(rows)["eligible/24h"]["observed"]

    assert observed["median"] == 1.0
    assert round(observed["mean"], 1) == -4.1
    assert observed["pct_losing"] == 10.0


def test_unresolved_is_counted_separately_and_only_assumed_lost_in_conservative_view():
    rows = [row("a", 10), row("b", 20), row("gone", status="unresolved", resolution="pair_missing")]
    item = summarize_outcomes(rows)["eligible/24h"]

    assert item["observed"]["n"] == 2
    assert item["unresolved"] == {"pair_missing": 1}
    assert item["conservative_unresolved_as_minus_100"]["n"] == 3
    assert item["conservative_unresolved_as_minus_100"]["median"] == 10.0


def test_outlier_stress_test_and_concentration():
    rows = [row(f"t{i}", -5) for i in range(49)] + [row("moon", 500)]
    observed = summarize_outcomes(rows)["eligible/24h"]["observed"]

    assert round(observed["mean"], 1) == 5.1
    assert observed["mean_without_top_1"] == -5.0
    assert observed["top_winner_share_of_gains"] == 1.0


def test_sol_excess_uses_each_signals_own_window_and_tokens_count_once():
    rows = [row("a", 10), row("a", 99), row("b", 2, day=1)]
    item = summarize_outcomes(rows)["eligible/24h"]

    assert item["tokens"] == 2
    assert item["sol_same_window"]["median"] == 2.0
    assert item["excess_vs_sol"]["mean"] == 4.0
    assert [h["n"] for h in item["halves"]] == [1, 1]


def test_throughput_sums_cycle_stats_and_rejection_reasons():
    stats = {"scored": 5, "passed_hard_filters": 1, "rejection_reasons": {"Liquidity is below USD 50,000": 4}}
    throughput = summarize_throughput(
        [{"day": "2026-10-20", "cycles_ok": 2, "cycles_error": 1, "cycles_unfinished": 0, "stats": [stats, stats]}]
    )

    assert throughput["totals"]["scored"] == 10
    assert throughput["totals"]["cycles_error"] == 1
    assert throughput["rejection_reasons"] == {"Liquidity is below USD 50,000": 8}
    report = {"throughput": throughput, "outcomes": {}, "enrolment": {}, "overlap": {"tokens_in_both": 0}}
    assert "Liquidity is below USD 50,000" in render_text(report)


def test_attention_flags_stale_failed_and_overdue_scans():
    now = T0 + timedelta(hours=2)
    healthy = {"last_ok_at": now - timedelta(minutes=10), "last_cycle": {"status": "ok"}, "errors_24h": 0, "overdue_checks": 0}
    assert scan_attention(healthy, now) == []

    problems = scan_attention(
        {"last_ok_at": now - timedelta(minutes=90), "last_cycle": {"status": "error", "error": "ProviderError"},
         "errors_24h": 3, "overdue_checks": 2},
        now,
    )
    assert problems == [
        "Last successful scan was 90 minutes ago.",
        "Most recent scan failed: ProviderError.",
        "3 failed scans in the last 24 hours.",
        "2 price checks are more than 30 minutes overdue.",
    ]
    assert scan_attention({}, now) == ["No successful scan recorded yet."]


def test_summary_csv_has_one_row_per_cohort_and_horizon_with_blanks_for_missing():
    rows = summary_rows(summarize_outcomes(
        [row("a", 10), row("b", kind="1h", status="pending", resolution=None)], now=T0 + timedelta(hours=1)))
    text = to_csv(rows, SUMMARY_COLUMNS)
    lines = text.strip().splitlines()

    assert lines[0].split(",") == list(SUMMARY_COLUMNS)
    assert len(lines) == 3
    # cohort, horizon, primary, tokens, observed, unresolved, pending, not_yet_due, waiting, on_time, late
    assert lines[1].startswith("eligible,1h,False,1,0,0,1,0,1,0,0,")


def test_outcome_status_buckets_sum_to_tokens():
    now = T0 + timedelta(days=1)
    rows = [
        row("future", status="pending", resolution=None, scheduled=now + timedelta(hours=3)),
        row("waiting", status="retryable", resolution=None, scheduled=now - timedelta(minutes=20)),
        row("on-time", 5, completed_after=timedelta(minutes=10)),
        row("late", -5, completed_after=timedelta(minutes=50)),
        row("gone", status="unresolved", resolution="pair_missing"),
        row("missed", status="unresolved", resolution="missed_window"),
    ]
    item = summarize_outcomes(rows, now=now)["eligible/24h"]

    assert item["status"] == {"not_yet_due": 1, "waiting": 1, "completed_on_time": 1,
                              "completed_late": 1, "unresolved": 2}
    assert sum(item["status"][k] for k in STATUS_BUCKETS) == item["tokens"] == 6
    assert item["unresolved"] == {"pair_missing": 1, "missed_window": 1}
    assert item["observed"]["n"] == 2 and item["observed"]["pct_positive"] == 50.0


def test_trigger_source_split_and_unknown_for_entries_before_it_was_recorded():
    rows = [row("a", 10, source="profile_feed"), row("b", -20, source="recheck"),
            row("c", 4, source="recheck"), row("old", 1, source="unknown")]
    item = summarize_outcomes(rows)["eligible/24h"]

    assert item["tokens_by_source"] == {"profile_feed": 1, "recheck": 2, "unknown": 1}
    assert item["observed_by_source"]["recheck"]["n"] == 2
    assert item["observed_by_source"]["recheck"]["median"] == -8.0
    assert enrolment(rows) == {"eligible": {"total": 4, "profile_feed": 1, "recheck": 2, "unknown": 1}}


def test_control_that_later_qualifies_is_counted_in_both_and_identified():
    rows = [row("x", 3, cohort="control"), row("x", 9, cohort="eligible", source="recheck"),
            row("y", 1, cohort="control")]
    report = summarize_outcomes(rows)

    assert report["control/24h"]["tokens"] == 2  # control history kept
    assert report["eligible/24h"]["tokens"] == 1
    assert cohort_overlap(rows) == {"tokens_in_both": 1, "tokens": ["x"]}


def test_same_cost_convention_for_both_cohorts_and_sol_is_zero_cost():
    rows = [row("e", 10), row("c", 10, cohort="control")]
    report = summarize_outcomes(rows)
    for key in ("eligible/24h", "control/24h"):
        item = report[key]
        assert item["after_costs"]["2pct_round_trip"]["median"] == 8.0
        assert item["after_costs"]["5pct_round_trip"]["median"] == 5.0
        # SOL moved +2% with no cost; excess is token-after-cost minus SOL.
        assert item["sol_same_window"]["median"] == 2.0
        assert item["excess_vs_sol"]["median"] == 8.0
        assert item["excess_vs_sol_after_costs"]["2pct_round_trip"]["median"] == 6.0


def test_text_report_states_cost_convention_and_three_questions():
    report = {
        "status": {}, "attention": [], "enrolment": {}, "overlap": {"tokens_in_both": 0},
        "throughput": summarize_throughput([]), "outcomes": summarize_outcomes([row("e", 10)]),
    }
    text = render_text(report)
    for phrase in ("1. IS IT WORKING?", "2. IS IT FINDING SUITABLE TOKENS?", "3. WHAT HAPPENED AFTERWARDS?",
                   "zero-cost buy-and-hold", "not verified trading returns", "not a causal comparison",
                   "still valid", "inside the collection window"):
        assert phrase in text
