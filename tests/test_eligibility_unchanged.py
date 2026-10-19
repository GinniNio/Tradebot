"""The reporting patch must not change a single eligibility decision.

Golden outputs were generated once from tradebot/strategy.py at 9edc647,
before the patch, and frozen in fixtures/eligibility_golden.json.
"""

import json
from pathlib import Path

import pytest

from tests.eligibility_cases import EXPLICIT, cases
from tradebot.market_watch import MarketWatchService, new_stats
from tradebot.strategy import _d, evaluate

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "eligibility_golden.json").read_text())


def test_every_decision_matches_the_pre_patch_scorer():
    # Eligibility, hard-filter pass/fail and score must match exactly.
    # Reporting labels added by the patch (missing_inputs, rejection_details)
    # are new output and are not compared.
    inputs = cases()
    assert len(inputs) == len(GOLDEN["outputs"])
    mismatches = [
        (case, expected, actual)
        for case, expected in zip(inputs, GOLDEN["outputs"])
        if (actual := [int((r := evaluate(case))["eligible"]), int(r["passed_hard_filters"]), str(r["score"])])
        != expected
    ]
    assert not mismatches, mismatches[:3]


@pytest.mark.parametrize(
    ("label", "eligible", "passed_hard"),
    [
        ("boundary liquidity_usd=49999.99", 0, 0),
        ("boundary liquidity_usd=50000", 1, 1),
        ("boundary price_change_1h_pct=25", 1, 1),
        ("boundary price_change_1h_pct=25.01", 0, 0),
        ("boundary market_cap_usd=25000000", 1, 1),
        ("boundary market_cap_usd=25000000.01", 0, 0),
        ("score 64.99", 0, 1),
        ("score 65.00", 1, 1),
        ("score 65.01", 1, 1),
        ("input liquidity_usd=None", 0, 0),
        ("input liquidity_usd='0'", 0, 0),
        ("input price_change_1h_pct='abc'", 0, 0),
        ("market cap missing, FDV used", 1, 1),
        ("high score, fails 1h filter", 0, 0),
        ("high score, fails market cap", 0, 0),
    ],
)
def test_named_cases_in_the_frozen_fixture(label, eligible, passed_hard):
    i = GOLDEN["explicit_labels"].index(label)
    assert GOLDEN["outputs"][i][:2] == [eligible, passed_hard]
    assert evaluate(EXPLICIT[i][1])["eligible"] is bool(eligible)


def test_missing_input_flags_only_describe_the_decision():
    for case in cases():
        r = evaluate(case)
        assert [d["reason"] for d in r["rejection_details"]] == r["rejection_reasons"]
        flagged = {d["reason"] for d in r["rejection_details"] if d["missing_input"]}
        # A reason is flagged as missing input only if that input really was absent.
        if _d(case["liquidity_usd"]) is None:
            assert "Liquidity is below USD 50,000" in flagged
        else:
            assert "Liquidity is below USD 50,000" not in flagged
        assert "Final score is below 65" not in flagged


def test_cycle_stats_split_rule_failures_from_missing_inputs():
    stats = new_stats()
    MarketWatchService._count(stats, evaluate({**cases()[0], "liquidity_usd": None}))
    MarketWatchService._count(stats, evaluate({**cases()[0], "liquidity_usd": "1000"}))
    assert stats["missing_inputs"] == {"Liquidity is below USD 50,000": 1}
    assert stats["rejection_reasons"]["Liquidity is below USD 50,000"] == 1
    assert stats["scored"] == 2
