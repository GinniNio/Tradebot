"""Deterministic inputs for the eligibility golden test.

Covers each hard-filter boundary, missing and malformed inputs, and a seeded
random spread. The golden outputs in fixtures/eligibility_golden.json were
produced by the scorer at commit 9edc647, before the reporting patch.
"""

import random

FIELD_VALUES = {
    "liquidity_usd": [None, "", "abc", "NaN", "0", "49999.99", "50000", "50001", "120000", "500000", "2000000"],
    "volume_1h_usd": [None, "0", "1000", "40000", "250000"],
    "volume_6h_usd": [None, "0", "6000", "200000", "600000"],
    "volume_24h_usd": [None, "Infinity", "99999.99", "100000", "900000", "6000000"],
    "price_change_1h_pct": [None, "-60", "0", "25", "25.01", "60"],
    "price_change_24h_pct": [None, "-30", "4.99", "5", "40", "80", "120", "120.01", "300"],
    "market_cap_usd": [None, "249999", "250000", "3000000", "25000000", "25000001"],
    "fdv_usd": [None, "1000000", "30000000"],
}


# Values inside or at the edge of every hard filter, so the score threshold
# (65) and the eligible/control boundary are exercised heavily.
NEAR_PASSING = {
    "liquidity_usd": ["50000", "80000", "150000", "300000", "500000", "900000"],
    "volume_1h_usd": ["5000", "30000", "80000", "250000"],
    "volume_6h_usd": ["30000", "120000", "300000", "600000"],
    "volume_24h_usd": ["100000", "400000", "900000", "2500000", "6000000"],
    "price_change_1h_pct": [None, "-10", "0", "10", "25"],
    "price_change_24h_pct": ["5", "20", "40", "60", "80", "120"],
    "market_cap_usd": [None, "250000", "1000000", "8000000", "25000000"],
    "fdv_usd": [None, "500000", "5000000"],
}


# A token that passes every filter with score 100.
STRONG = {
    "liquidity_usd": "500000", "volume_1h_usd": "250000", "volume_6h_usd": "600000",
    "volume_24h_usd": "6000000", "price_change_1h_pct": "10", "price_change_24h_pct": "80",
    "market_cap_usd": "5000000", "fdv_usd": None,
}
# Exactly 65: liquidity 30 + turnover 25 + momentum 10 (24h +35%) + acceleration 0.
AT_65 = {**STRONG, "price_change_24h_pct": "35", "volume_1h_usd": "10000"}


def _explicit():
    out = []
    boundaries = {
        "liquidity_usd": ("49999.99", "50000", "50000.01"),
        "volume_24h_usd": ("99999.99", "100000", "100000.01"),
        "market_cap_usd": ("249999.99", "250000", "250000.01", "24999999.99", "25000000", "25000000.01"),
        "price_change_24h_pct": ("4.99", "5", "5.01", "119.99", "120", "120.01"),
        "price_change_1h_pct": ("24.99", "25", "25.01"),
    }
    for field, values in boundaries.items():
        for value in values:
            out.append((f"boundary {field}={value}", {**STRONG, field: value}))
    for label, change in (("score 64.99", "34.97"), ("score 65.00", "35"), ("score 65.01", "35.03")):
        out.append((label, {**AT_65, "price_change_24h_pct": change}))
    for field in ("liquidity_usd", "volume_24h_usd", "market_cap_usd", "price_change_24h_pct",
                  "price_change_1h_pct", "volume_1h_usd", "volume_6h_usd"):
        for bad in (None, "0", "", "abc", "NaN", "Infinity", "-1"):
            out.append((f"input {field}={bad!r}", {**STRONG, field: bad}))
    out.append(("market cap missing, FDV used", {**STRONG, "market_cap_usd": None, "fdv_usd": "5000000"}))
    out.append(("market cap and FDV both missing", {**STRONG, "market_cap_usd": None, "fdv_usd": None}))
    out.append(("high score, fails 1h filter", {**STRONG, "price_change_1h_pct": "40"}))
    out.append(("high score, fails market cap", {**STRONG, "market_cap_usd": "30000000"}))
    out.append(("high score, fails 24h change", {**STRONG, "price_change_24h_pct": "150"}))
    return out


EXPLICIT = _explicit()


def cases(n_random=4000, n_near=3000, seed=20261019):
    rng = random.Random(seed)
    base = {k: v[-2] for k, v in FIELD_VALUES.items()}
    out = [case for _label, case in EXPLICIT]
    # Vary one field at a time from a passing-ish base: every boundary value.
    for field, values in FIELD_VALUES.items():
        for value in values:
            out.append({**base, field: value})
    for _ in range(n_random):
        out.append({k: rng.choice(v) for k, v in FIELD_VALUES.items()})
    for _ in range(n_near):
        out.append({k: rng.choice(v) for k, v in NEAR_PASSING.items()})
    return out
