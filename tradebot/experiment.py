"""Frozen rules for the automatic swing_quality measurement experiment.

Everything here is stored in strategy_versions.config_json alongside the
scoring thresholds, so a report can always say which rules produced it.
Changing any value means a new STRATEGY_VERSION and a fresh evaluation period.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

PRIMARY_HORIZON = "24h"

# check_kind -> (horizon, tolerance). A price observed later than
# scheduled_at + tolerance is not a price for that horizon: the check is
# closed as unresolved instead.
HORIZONS: dict[str, tuple[timedelta, timedelta]] = {
    "1h": (timedelta(hours=1), timedelta(minutes=15)),
    "24h": (timedelta(hours=24), timedelta(hours=2)),
    "3d": (timedelta(days=3), timedelta(hours=6)),
    "7d": (timedelta(days=7), timedelta(hours=12)),
}

RETRY_DELAY = timedelta(minutes=5)
STALE_CLAIM_AFTER = timedelta(minutes=10)

# Comparison cohort: tokens that pass all hard filters but score below the
# threshold. Membership is a fixed hash of the token address, decided before
# any outcome is known.
CONTROL_SAMPLE_ONE_IN = 2

# Stored-token recheck: revisit tokens first seen this long ago.
RECHECK_MIN_AGE = timedelta(days=1)
RECHECK_MAX_AGE = timedelta(days=7)
RECHECK_TOKENS_PER_CYCLE = 300

WSOL_MINT = "So11111111111111111111111111111111111111112"


def in_control_sample(token_address: str) -> bool:
    digest = hashlib.sha256(f"control:{token_address}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % CONTROL_SAMPLE_ONE_IN == 0


def config_json() -> dict[str, Any]:
    return {
        "primary_horizon": PRIMARY_HORIZON,
        "horizons": {
            kind: {"after_seconds": int(h.total_seconds()), "tolerance_seconds": int(t.total_seconds())}
            for kind, (h, t) in HORIZONS.items()
        },
        "control_sample_one_in": CONTROL_SAMPLE_ONE_IN,
        "recheck_min_age_seconds": int(RECHECK_MIN_AGE.total_seconds()),
        "recheck_max_age_seconds": int(RECHECK_MAX_AGE.total_seconds()),
        "recheck_tokens_per_cycle": RECHECK_TOKENS_PER_CYCLE,
        "benchmark": "dexscreener_sol_usd_top_pair",
    }
