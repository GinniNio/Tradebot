from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class SwingQualityConfig:
    liquidity_min_usd: Decimal = Decimal(50000)
    volume_24h_min_usd: Decimal = Decimal(100000)
    market_cap_min_usd: Decimal = Decimal(250000)
    market_cap_max_usd: Decimal = Decimal(25000000)
    price_change_24h_min_pct: Decimal = Decimal(5)
    price_change_24h_max_pct: Decimal = Decimal(120)
    price_change_1h_max_pct: Decimal = Decimal(25)
    score_min: Decimal = Decimal(65)
    cooldown_hours: int = 24
    shortlist_max: int = 3

    def json(self) -> dict[str, Any]:
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in asdict(self).items()
        }


FROZEN_CONFIG = SwingQualityConfig()
STRATEGY_KEY = "swing_quality"
STRATEGY_VERSION = "market_watch_v1"


def _d(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (ValueError, TypeError):
        return None


def _bounded(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(Decimal(0), min(Decimal(1), (value - low) / (high - low)))


def evaluate(
    snapshot: dict[str, Any], config: SwingQualityConfig = FROZEN_CONFIG
) -> dict[str, Any]:
    liquidity = _d(snapshot.get("liquidity_usd"))
    volume_1h = _d(snapshot.get("volume_1h_usd"))
    volume_6h = _d(snapshot.get("volume_6h_usd"))
    volume_24h = _d(snapshot.get("volume_24h_usd"))
    change_1h = _d(snapshot.get("price_change_1h_pct"))
    change_24h = _d(snapshot.get("price_change_24h_pct"))
    market_cap = _d(snapshot.get("market_cap_usd") or snapshot.get("fdv_usd"))
    rejects: list[str] = []
    checks = [
        (
            liquidity is None or liquidity < config.liquidity_min_usd,
            "Liquidity is below USD 50,000",
        ),
        (
            volume_24h is None or volume_24h < config.volume_24h_min_usd,
            "24-hour volume is below USD 100,000",
        ),
        (
            market_cap is None
            or market_cap < config.market_cap_min_usd
            or market_cap > config.market_cap_max_usd,
            "Market cap/FDV is outside USD 250,000–25,000,000",
        ),
        (
            change_24h is None
            or change_24h < config.price_change_24h_min_pct
            or change_24h > config.price_change_24h_max_pct,
            "24-hour price change is outside +5%–+120%",
        ),
        (
            change_1h is None or change_1h > config.price_change_1h_max_pct,
            "1-hour price change exceeds +25%",
        ),
    ]
    rejects.extend(reason for failed, reason in checks if failed)

    liquidity_score = (
        Decimal(0)
        if liquidity is None
        else Decimal(30)
        * _bounded(liquidity, config.liquidity_min_usd, Decimal(500000))
    )
    turnover = (
        Decimal(0) if not liquidity or volume_24h is None else volume_24h / liquidity
    )
    turnover_score = Decimal(25) * _bounded(turnover, Decimal(1), Decimal(12))
    momentum_score = (
        Decimal(0)
        if change_24h is None
        else Decimal(25)
        * _bounded(change_24h, config.price_change_24h_min_pct, Decimal(80))
    )
    acceleration = (
        Decimal(0)
        if not volume_6h or volume_1h is None
        else volume_1h * Decimal(6) / volume_6h
    )
    acceleration_score = Decimal(20) * _bounded(
        acceleration, Decimal("0.8"), Decimal("2.5")
    )
    components = {
        "liquidity": liquidity_score.quantize(Decimal("0.01")),
        "turnover": turnover_score.quantize(Decimal("0.01")),
        "momentum": momentum_score.quantize(Decimal("0.01")),
        "volume_acceleration": acceleration_score.quantize(Decimal("0.01")),
    }
    score = sum(components.values(), Decimal(0))
    if score < config.score_min:
        rejects.append("Final score is below 65")
    reasons = [
        f"{name.replace('_', ' ').title()}: {points}/{maximum} points"
        for (name, points), maximum in zip(components.items(), (30, 25, 25, 20))
    ]
    return {
        "eligible": not rejects,
        "score": score,
        "score_inputs": {"turnover_24h": turnover, "volume_acceleration": acceleration},
        "score_components": components,
        "rejection_reasons": rejects,
        "explanations": reasons,
    }


def rank_eligible(items: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    ranked = sorted(
        (item for item in items if item.get("eligible")),
        key=lambda x: (-Decimal(str(x["score"])), str(x.get("token_address", ""))),
    )[:limit]
    return [{**item, "rank": index} for index, item in enumerate(ranked, 1)]
