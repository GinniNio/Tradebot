from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradebot.checkpoint import (
    band,
    option_a_snapshot,
    summarize_control,
    summarize_graduates,
    summarize_passers,
)


def row(token, *, liquidity="500000", volume_1h="1000", change_24h="80", stored_score=None):
    return {
        "token_address": token,
        "stored_score": stored_score,
        "liquidity_usd": Decimal(liquidity),
        "volume_1h_usd": Decimal(volume_1h),
        "volume_6h_usd": Decimal(6000),
        "volume_24h_usd": Decimal(6000000),
        "price_change_1h_pct": Decimal(5),
        "price_change_24h_pct": Decimal(change_24h),
        "market_cap_usd": Decimal(1000000),
        "fdv_usd": None,
    }


# Acceleration is 1.0x in every row (2.35 points).
# liquidity 30 + turnover 25 + momentum 25 = 82.35
HIGH = {}
# liquidity 30 + turnover 25 + momentum 0 = 57.35
MID = {"change_24h": "5"}
# liquidity 0 (at the 50k floor) + turnover 25 + momentum 0 = 27.35
LOW = {"liquidity": "50000", "change_24h": "5"}


def test_band_edges():
    assert [band(Decimal(x)) for x in ("49.99", "50", "64.99", "65")] == ["<50", "50-64", "50-64", "65+"]


def test_passers_bands_and_entry_type():
    rows = [
        row("a", **LOW), row("a", **MID), row("a", **HIGH),  # seen below 50 first
        row("b", **MID), row("b", **LOW),                    # first stored pass already 50+
        row("c", liquidity="1000"),                          # fails a hard filter: ignored
    ]
    out = summarize_passers(rows)
    assert out["observations_by_band"] == {"<50": 2, "50-64": 2, "65+": 1}
    assert out["tokens_by_best_band"] == {"<50": 0, "50-64": 1, "65+": 1}
    assert out["tokens_reaching_50_by_entry"] == {"observed_crossing": 1, "first_seen_above": 1}


def test_control_score_recomputed_from_enrolment_snapshot():
    rows = [
        row("a", **LOW, stored_score=Decimal("27.35")),
        row("b", **MID, stored_score=Decimal("27.35")),  # enrolled on a later observation
        {**row("c"), "liquidity_usd": None, "price_change_24h_pct": None},
    ]
    out = summarize_control(rows)
    assert out["score_at_entry_by_band"] == {"<50": 1, "50-64": 1, "65+": 0}
    assert out["stored_score_differs_from_entry"] == 1
    assert out["snapshot_missing"] == 1


def pool(token, liquidity, *, h24="0", age_days=10, change_24h="20"):
    created = (datetime.now(UTC) - timedelta(days=age_days)).timestamp() * 1000
    return {
        "baseToken": {"address": token},
        "liquidity": {"usd": liquidity},
        "volume": {"h1": "100", "h6": "600", "h24": h24},
        "priceChange": {"h1": "1", "h24": change_24h},
        "marketCap": "1000000",
        "pairCreatedAt": int(created),
    }


def test_option_a_sums_non_dust_pools_and_takes_price_from_deepest():
    pairs = [
        pool("t", "100000", h24="200000", change_24h="30"),
        pool("t", "40000", h24="50000", change_24h="99"),
        pool("t", "5000", h24="999999"),         # dust: ignored
        pool("other", "900000", h24="999999"),   # quote side or another token: ignored
    ]
    snap = option_a_snapshot(pairs, "t")
    assert snap["liquidity_usd"] == Decimal(140000)
    assert snap["volume_24h_usd"] == Decimal(250000)
    assert snap["price_change_24h_pct"] == "30"
    assert snap["deepest_liquidity_usd"] == Decimal(100000)
    assert option_a_snapshot([pool("t", "5000")], "t") is None


def test_graduates_bucketed_by_first_seen_age():
    now = datetime.now(UTC)
    first_seen = {"young": now - timedelta(days=8), "old": now - timedelta(days=40), "gone": now - timedelta(days=9)}
    pairs = {
        "young": [pool("young", "100000", h24="500000")],
        "old": [pool("old", "30000", h24="500000")],
    }
    out = summarize_graduates(first_seen, pairs)
    assert out["7-14d"]["first_seen"] == 2
    assert out["7-14d"]["passes_hard_filters_now"] == 1
    assert out["30d+"] == {"first_seen": 1, "listed_pool_10k_plus": 1, "deepest_pool_7d_plus": 1}
