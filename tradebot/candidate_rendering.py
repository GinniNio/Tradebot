from __future__ import annotations

import html
from typing import Any


def render_rejected_audit_rows(audit_rows: list[dict[str, Any]]) -> str:
    audit_items = []
    for row in audit_rows:
        esc = lambda value: html.escape(str(value if value is not None else "—"))
        hard_rejects = row.get("hard_rejects_json") or []
        if isinstance(hard_rejects, list):
            hard_rejects_text = ", ".join(str(item) for item in hard_rejects) or "—"
        else:
            hard_rejects_text = str(hard_rejects)
        audit_items.append(f"""<li><strong>{esc(row.get('token_symbol') or row.get('token_address'))}</strong> — {esc(row.get('state'))}; outcomes {esc(row.get('outcome_completed'))}/{esc(row.get('outcome_total'))} (read-only)<br>
        Pair: <a href="https://dexscreener.com/solana/{esc(row.get("selected_pair"))}">{esc(row.get("selected_pair"))}</a> · Liquidity: ${esc(row.get("liquidity_usd"))}<br>
        Volume 1h/24h: ${esc(row.get("volume_1h_usd"))} / ${esc(row.get("volume_24h_usd"))} · Movement 1h/24h: {esc(row.get("price_change_1h_pct"))}% / {esc(row.get("price_change_24h_pct"))}%<br>
        hard_rejects_json: {esc(hard_rejects_text)}</li>""")
    return "".join(audit_items) or "<li>No rejected or expired candidates</li>"
