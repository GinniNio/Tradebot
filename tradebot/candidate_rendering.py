from __future__ import annotations

import html
from decimal import Decimal, InvalidOperation
from typing import Any


def _compact_dollars(value: Any) -> str:
    if value is None:
        return "—"
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return "—"
    if abs(amount) >= Decimal(1000):
        return f"${amount / Decimal(1000):.1f}k"
    return f"${amount:.1f}"


def _movement(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{Decimal(str(value)):.1f}%"
    except (InvalidOperation, ValueError):
        return "—"


def _rejection_reasons(value: Any) -> str:
    if isinstance(value, list):
        reasons = value
    elif value:
        reasons = [value]
    else:
        reasons = ["—"]
    return "<ul>" + "".join(f"<li>{html.escape(str(reason))}</li>" for reason in reasons) + "</ul>"


def render_rejected_audit_rows(audit_rows: list[dict[str, Any]]) -> str:
    audit_items = []
    for row in audit_rows:
        esc = lambda value: html.escape(str(value if value is not None else "—"))
        reasons = _rejection_reasons(row.get("hard_rejects_json"))
        audit_items.append(f"""<li><strong>{esc(row.get('token_symbol') or row.get('token_address'))}</strong> — {esc(row.get('state'))}; outcomes {esc(row.get('outcome_completed'))}/{esc(row.get('outcome_total'))} (read-only)<br>
        Pair: <a href="https://dexscreener.com/solana/{esc(row.get("selected_pair"))}">View on Dexscreener</a> · Liquidity: {_compact_dollars(row.get("liquidity_usd"))}<br>
        Volume 1h/24h: {_compact_dollars(row.get("volume_1h_usd"))} / {_compact_dollars(row.get("volume_24h_usd"))} · Movement 1h/24h: {_movement(row.get("price_change_1h_pct"))} / {_movement(row.get("price_change_24h_pct"))}<br>
        Rejected because: {reasons}</li>""")
    return "".join(audit_items) or "<li>No rejected or expired candidates</li>"
