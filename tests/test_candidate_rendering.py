from decimal import Decimal
from uuid import uuid4

from tradebot.candidate_rendering import render_rejected_audit_rows


def test_rejected_candidate_audit_row_renders_rejection_and_market_fields():
    html = render_rejected_audit_rows(
        [
            {
                "id": uuid4(),
                "token_symbol": "TOK",
                "token_address": "TokenAddress111111111111111111111111111111",
                "state": "rejected",
                "selected_pair": "SelectedPair11111111111111111111111111111",
                "hard_rejects_json": [
                    "Liquidity is below USD 50,000",
                    "1-hour price change exceeds +25%",
                ],
                "liquidity_usd": Decimal(16100),
                "volume_1h_usd": Decimal(4800),
                "volume_24h_usd": Decimal(37200),
                "price_change_1h_pct": Decimal("-16.64"),
                "price_change_24h_pct": Decimal("72.25"),
                "outcome_completed": 0,
                "outcome_total": 0,
            }
        ]
    )

    assert "Liquidity is below USD 50,000" in html
    assert "1-hour price change exceeds +25%" in html
    assert "Liquidity: $16.1k" in html
    assert "Volume 1h/24h: $4.8k / $37.2k" in html
    assert "Movement 1h/24h: -16.6% / 72.2%" in html
    assert ">View on Dexscreener</a>" in html
    assert ">SelectedPair11111111111111111111111111111</a>" not in html
    assert "Rejected because:" in html
    assert "<ul><li>Liquidity is below USD 50,000</li><li>1-hour price change exceeds +25%</li></ul>" in html
    assert "hard_rejects_json:" not in html
