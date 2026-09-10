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
                "liquidity_usd": Decimal(49000),
                "volume_1h_usd": Decimal(1234),
                "volume_24h_usd": Decimal(98765),
                "price_change_1h_pct": Decimal("26.5"),
                "price_change_24h_pct": Decimal("72.25"),
                "outcome_completed": 0,
                "outcome_total": 0,
            }
        ]
    )

    assert "Liquidity is below USD 50,000" in html
    assert "1-hour price change exceeds +25%" in html
    assert "Liquidity: $49000" in html
    assert "Volume 1h/24h: $1234 / $98765" in html
    assert "Movement 1h/24h: 26.5% / 72.25%" in html
    assert "SelectedPair11111111111111111111111111111" in html
    assert "Rejected because:" in html
    assert "hard_rejects_json:" not in html
