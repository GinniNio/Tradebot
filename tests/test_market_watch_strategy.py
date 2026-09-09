from decimal import Decimal

import pytest

from tradebot.dexscreener import normalize_pair, select_pair
from tradebot.market_watch import valid_price
from tradebot.strategy import evaluate, rank_eligible


def passing_snapshot(**overrides):
    values = {
        "liquidity_usd": "500000",
        "volume_1h_usd": "250000",
        "volume_6h_usd": "600000",
        "volume_24h_usd": "6000000",
        "market_cap_usd": "1000000",
        "price_change_1h_pct": "10",
        "price_change_24h_pct": "80",
    }
    values.update(overrides)
    return values


def test_frozen_filter_and_component_weights():
    result = evaluate(passing_snapshot())
    assert result["eligible"] is True
    assert result["score"] == Decimal("100.00")
    assert result["score_components"] == {
        "liquidity": Decimal("30.00"),
        "turnover": Decimal("25.00"),
        "momentum": Decimal("25.00"),
        "volume_acceleration": Decimal("20.00"),
    }


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"liquidity_usd": "49999"}, "Liquidity"),
        ({"volume_24h_usd": "99999"}, "24-hour volume"),
        ({"market_cap_usd": "25000001"}, "Market cap/FDV"),
        ({"price_change_24h_pct": "4.99"}, "24-hour price change"),
        ({"price_change_1h_pct": "25.01"}, "1-hour price change"),
    ],
)
def test_hard_filter_reasons_are_human_readable(change, reason):
    result = evaluate(passing_snapshot(**change))
    assert result["eligible"] is False
    assert any(reason in item for item in result["rejection_reasons"])


def test_rank_is_deterministic_and_limited_to_three():
    items = [
        {"eligible": True, "score": score, "token_address": token}
        for score, token in [(70, "d"), (90, "b"), (90, "a"), (80, "c")]
    ]
    assert [(row["token_address"], row["rank"]) for row in rank_eligible(items)] == [
        ("a", 1),
        ("b", 2),
        ("c", 3),
    ]


def test_pair_selection_prefers_strongest_usable_liquidity():
    pairs = [
        {"pairAddress": "weak", "priceUsd": "1", "liquidity": {"usd": "100"}},
        {"pairAddress": "bad", "priceUsd": None, "liquidity": {"usd": "999"}},
        {"pairAddress": "strong", "priceUsd": "2", "liquidity": {"usd": "500"}},
    ]
    assert select_pair(pairs)["pairAddress"] == "strong"


def test_normalization_retains_raw_payload():
    pair = {
        "pairAddress": "p",
        "priceUsd": "1",
        "baseToken": {"address": "t", "symbol": "TOK"},
        "liquidity": {"usd": 1},
    }
    assert normalize_pair(pair)["raw_payload_json"] is pair


@pytest.mark.parametrize("value", [None, 0, "0", "-1", "nan", "inf", "bad"])
def test_invalid_outcome_prices_never_produce_a_value(value):
    assert valid_price(value) is None
