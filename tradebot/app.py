from __future__ import annotations

import asyncio
import contextlib
import html
import os
import secrets
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from tradebot import verdict
from tradebot.advisory_lock import AdvisoryLock, InterfaceError, PostgresError
from tradebot.candidate_rendering import render_rejected_audit_rows
from tradebot.config import Settings, load_settings
from tradebot.db import Database
from tradebot.dexscreener import DexscreenerClient
from tradebot.health import build_health
from tradebot.market_watch import MarketWatchService
from tradebot.repository import InvalidCandidateTransition, Repository
from tradebot.scanner import ManagedScanner, ScannerState
from tradebot.serialization import api_value

# How often a standby instance retries the scanner lock. During a deploy the
# old instance holds it until Render stops it; without retries the new
# instance would stay in standby and nothing would scan.
LOCK_RETRY_SECONDS = float(os.environ.get("SCANNER_LOCK_RETRY_SECONDS", "30"))


async def _try_lock(lock: AdvisoryLock) -> bool:
    lock.last_error_category = None
    try:
        acquired = await lock.acquire()
    except (OSError, TimeoutError, RuntimeError, InterfaceError, PostgresError) as exc:
        lock.last_error_category = type(exc).__name__
        acquired = False
    if not acquired:
        await lock.release()
    return acquired


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    db = Database(settings)
    await db.connect()
    scanner_state = ScannerState()
    lock = AdvisoryLock(settings)
    app.state.settings = settings
    app.state.db = db
    app.state.scanner_state = scanner_state
    app.state.scanner_lock = lock
    app.state.scanner = None

    def start_scanner() -> None:
        market_watch = MarketWatchService(
            db, DexscreenerClient(settings.dexscreener.budget), settings.git_commit_sha
        )
        scanner_state.last_cycle_error = None
        app.state.scanner = ManagedScanner(
            scanner_state, market_watch.run_cycle, settings.scanner_interval_seconds
        )
        app.state.scanner.start()

    def mark_standby() -> None:
        scanner_state.status = "standby"
        scanner_state.last_cycle_error = (
            f"lock_error:{lock.last_error_category}" if lock.last_error_category else None
        )

    async def wait_for_lock() -> None:
        while True:
            await asyncio.sleep(LOCK_RETRY_SECONDS)
            if await _try_lock(lock):
                start_scanner()
                return
            mark_standby()

    standby_task: asyncio.Task | None = None
    if await _try_lock(lock):
        start_scanner()
    else:
        mark_standby()
        standby_task = asyncio.create_task(wait_for_lock(), name="scanner_lock_retry")
    try:
        yield
    finally:
        try:
            if standby_task and not standby_task.done():
                standby_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await standby_task
            if app.state.scanner:
                await app.state.scanner.shutdown(timeout_seconds=5.0)
        finally:
            try:
                await lock.release()
            finally:
                await db.close()


app = FastAPI(
    title="Tradebot Managed Research Service", version="0.1.0", lifespan=lifespan
)
basic = HTTPBasic(auto_error=False)


def operator_auth(credentials: HTTPBasicCredentials | None = Depends(basic)) -> str:  # noqa: B008
    settings: Settings = app.state.settings
    expected_user = settings.operator_dashboard_username
    expected_password = settings.operator_dashboard_password
    valid = bool(
        credentials
        and expected_user
        and expected_password
        and secrets.compare_digest(
            credentials.username.encode(), expected_user.encode()
        )
        and secrets.compare_digest(
            credentials.password.encode(), expected_password.encode()
        )
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def same_origin(request: Request) -> None:
    # Compare hosts only. Render terminates TLS, so the app sees http:// while
    # the browser's Origin is https://; comparing full URLs rejected every
    # dashboard button. Another site's Origin still has a different host.
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
        raise HTTPException(status_code=403, detail="Cross-origin mutation rejected")


class DecisionBody(BaseModel):
    action: str
    note: str | None = None


class PauseBody(BaseModel):
    research_paused: bool


@app.get("/healthz")
async def healthz():
    settings: Settings = app.state.settings
    db: Database = app.state.db
    scanner_state: ScannerState = app.state.scanner_state
    payload = await build_health(settings, db, scanner_state)
    status_code = 200 if payload.get("status") == "ok" else 503
    return JSONResponse(payload, status_code=status_code)


@app.get("/")
async def root():
    return RedirectResponse("/report", status_code=status.HTTP_307_TEMPORARY_REDIRECT)


PAGE_STYLE = """body{font:16px system-ui;max-width:1100px;margin:0 auto;padding:1rem}
nav a{margin-right:1rem}article{border:1px solid #ccc;border-radius:10px;padding:1rem;margin:1rem 0}
.scroll{overflow-x:auto}table{border-collapse:collapse;font-size:14px}
th,td{border-bottom:1px solid #ddd;padding:.3rem .5rem;text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}.bad{color:#b00020}.ok{color:#126b2f}
.primary{background:#fff7d6}"""

NAV = '<nav><a href="/report">Research report</a><a href="/candidates">Market watch</a></nav>'


def _cell(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:+.1f}"
    return html.escape(str(value))


def _status_block(report: dict) -> str:
    status = report["status"]
    scanner = app.state.scanner_state
    last_ok = status.get("last_ok_at")
    last = status.get("last_cycle") or {}
    attention = report["attention"]
    if scanner.status != "active":
        attention = [f"Scanner is {scanner.status} on this instance ({scanner.last_cycle_error or 'no error recorded'})."] + attention
    items = "".join(f'<li class="bad">{html.escape(p)}</li>' for p in attention) or (
        '<li class="ok">Nothing needs attention.</li>'
    )
    return f"""<article><h2>Status</h2>
    <p>Last successful scan: <strong>{_cell(last_ok.strftime("%Y-%m-%d %H:%M UTC") if last_ok else None)}</strong>
    · Last scan: {_cell(last.get("status"))} · Scans in 24h: {_cell(status.get("ok_24h"))} ok,
    {_cell(status.get("errors_24h"))} failed · Version: {html.escape(app.state.settings.git_commit_sha[:12])}</p>
    <ul>{items}</ul></article>"""


async def _report(days: int) -> dict:
    async with app.state.db.acquire() as conn:
        return await verdict.collect_from_conn(conn, days)


@app.get("/report", response_class=HTMLResponse)
async def report_page(days: int = 7, _user: str = Depends(operator_auth)):
    days = max(1, min(days, 90))
    report = await _report(days)
    throughput = report["throughput"]
    day_rows = "".join(
        "<tr>" + "".join(f"<td>{_cell(d.get(k, 0))}</td>" for k in (
            "day", "cycles_ok", "cycles_error", "scored", "passed_hard_filters", "eligible",
            "eligible_tokens_started", "control_tokens_started", "recheck_tokens",
            "outcomes_completed", "outcomes_unresolved", "provider_errors")) + "</tr>"
        for d in throughput["days"]
    ) or '<tr><td colspan="12">No scans recorded in this period.</td></tr>'
    reasons = "".join(
        f"<tr><td>{html.escape(reason)}</td><td>{count}</td></tr>"
        for reason, count in throughput["rejection_reasons"].items()
    ) or '<tr><td colspan="2">None yet.</td></tr>'
    summary = verdict.summary_rows(report["outcomes"])
    shown = ("cohort", "horizon", "tokens", "observed", "unresolved", "pending", "median_pct",
             "mean_pct", "pct_losing", "worst_pct", "mean_without_top_1",
             "conservative_median_pct", "excess_vs_sol_median_pct", "after_2pct_median",
             "first_half_n", "first_half_median", "second_half_n", "second_half_median")
    outcome_rows = "".join(
        f'<tr class="{"primary" if row["primary"] else ""}">'
        + "".join(f"<td>{_cell(row[k])}</td>" for k in shown) + "</tr>"
        for row in summary
    ) or f'<tr><td colspan="{len(shown)}">No outcomes recorded yet.</td></tr>'
    return HTMLResponse(f"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Tradebot Research Report</title><style>{PAGE_STYLE}</style></head><body>{NAV}
    <h1>Research report: {html.escape(verdict.STRATEGY_VERSION)}</h1>
    <p>Market-price research only. Returns are Dexscreener price changes, not executable trades.
    Unresolved checks have no price; they count as -100% only in the conservative column.</p>
    {_status_block(report)}
    <p><a href="/report/summary.csv?days={days}">Download summary CSV</a> ·
    <a href="/report/observations.csv">Download observations CSV</a> ·
    Period: <a href="/report?days=7">7 days</a> <a href="/report?days=30">30 days</a></p>
    <h2>Outcomes (primary horizon highlighted)</h2><div class="scroll"><table>
    <tr>{"".join(f"<th>{html.escape(k)}</th>" for k in shown)}</tr>{outcome_rows}</table></div>
    <h2>Collection, last {days} days</h2><div class="scroll"><table>
    <tr><th>day</th><th>scans ok</th><th>failed</th><th>scored</th><th>passed hard filters</th>
    <th>eligible</th><th>eligible started</th><th>control started</th><th>rechecked</th>
    <th>checks done</th><th>unresolved</th><th>provider errors</th></tr>{day_rows}</table></div>
    <h2>Rejection reasons, last {days} days</h2><p>One token can fail several rules.</p>
    <div class="scroll"><table><tr><th>reason</th><th>count</th></tr>{reasons}</table></div>
    </body></html>""")


def _csv_response(body: str, name: str) -> Response:
    return Response(
        body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.get("/report/summary.csv")
async def report_summary_csv(days: int = 7, _user: str = Depends(operator_auth)):
    report = await _report(max(1, min(days, 90)))
    body = verdict.to_csv(verdict.summary_rows(report["outcomes"]), verdict.SUMMARY_COLUMNS)
    return _csv_response(body, f"tradebot-summary-{verdict.STRATEGY_VERSION}.csv")


@app.get("/report/observations.csv")
async def report_observations_csv(_user: str = Depends(operator_auth)):
    report = await _report(7)
    body = verdict.to_csv(report["observations"], verdict.OBSERVATION_COLUMNS)
    return _csv_response(body, f"tradebot-observations-{verdict.STRATEGY_VERSION}.csv")


async def _queue_rows() -> tuple[list[dict], list[dict]]:
    async with app.state.db.acquire() as conn:
        repository = Repository(conn)

        async def enrich(source_rows):
            rows = []
            for row in source_rows:
                item = api_value(dict(row))
                item["outcomes"] = api_value(
                    [
                        dict(outcome)
                        for outcome in await repository.list_candidate_outcomes(
                            row["id"]
                        )
                    ]
                )
                rows.append(item)
            return rows

        active = await enrich(await repository.list_candidates(3))
        audit = await enrich(await repository.list_rejected_candidates(20))
        return active, audit


@app.get("/api/candidates")
async def api_candidates(_user: str = Depends(operator_auth)):
    candidates, rejected_audit = await _queue_rows()
    return {
        "disclaimer": "Market-screening research only; not a trade recommendation.",
        "candidates": candidates,
        "rejected_audit": rejected_audit,
    }


@app.get("/candidates", response_class=HTMLResponse)
async def candidates_dashboard(_user: str = Depends(operator_auth)):
    rows, audit_rows = await _queue_rows()
    cards = []
    for row in rows:
        esc = lambda value: html.escape(str(value if value is not None else "—"))
        reasons = (
            ", ".join(
                row.get("hard_rejects_json") or row.get("ranking_reasons_json") or []
            )
            or "Passed frozen screen"
        )
        outcome_lines = (
            "".join(
                f"<li>{esc(outcome.get('check_kind'))}: {esc(outcome.get('status'))}; market-price change {esc(outcome.get('market_price_change_pct'))}%</li>"
                for outcome in row.get("outcomes", [])
            )
            or "<li>No outcomes scheduled</li>"
        )
        cards.append(f"""<article><h2>#{esc(row.get("rank"))} {esc(row.get("token_symbol"))}</h2>
        <p><a href="https://dexscreener.com/solana/{esc(row.get("selected_pair"))}">{esc(row.get("token_address"))}</a></p>
        <p>Pair: {esc(row.get("selected_pair"))} · Score: {esc(row.get("score"))}</p>
        <p>Liquidity: ${esc(row.get("liquidity_usd"))} · Volume 1h/24h: ${esc(row.get("volume_1h_usd"))} / ${esc(row.get("volume_24h_usd"))}</p>
        <p>Movement 1h/24h: {esc(row.get("price_change_1h_pct"))}% / {esc(row.get("price_change_24h_pct"))}% · Market cap/FDV: {esc(row.get("market_cap_usd") or row.get("fdv_usd"))}</p>
        <p>{esc(reasons)} · Decision: {esc(row.get("operator_action"))} · Outcomes: {esc(row.get("outcome_completed"))}/{esc(row.get("outcome_total"))}</p>
        <ul>{outcome_lines}</ul><p>Market-price research outcomes; not executable returns.</p>
        <p><strong>Exitability: not assessed in this release</strong><br><strong>Security: not assessed in this release</strong></p>
        <p><button onclick="decide('{esc(row.get("id"))}','watch')">Watch</button>
        <button onclick="decide('{esc(row.get("id"))}','reject')">Reject</button></p></article>""")
    content = "".join(cards) or "<p>No candidates passed today</p>"
    audit = render_rejected_audit_rows(audit_rows)
    return HTMLResponse(f"""<!doctype html><html><head><title>Tradebot Market Watch</title>
    <style>body{{font:16px system-ui;max-width:960px;margin:2rem auto;padding:1rem}}article{{border:1px solid #ccc;border-radius:10px;padding:1rem;margin:1rem 0}}</style></head>
    <body>{NAV}<h1>Daily Market Watch</h1><p>Market-screening research only. Not a trade recommendation or executable return.</p>
    <p><button onclick="pause(true)">Pause research</button> <button onclick="pause(false)">Resume research</button></p>{content}
    <h2>Rejected and expired audit history</h2><ul>{audit}</ul>
    <script>async function decide(id,action){{let note=prompt('Optional note')||null;let r=await fetch('/api/candidates/'+id+'/decision',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{action,note}})}});if(r.ok)location.reload();else alert('Decision failed');}}
    async function pause(research_paused){{let r=await fetch('/api/operator/research-paused',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{research_paused}})}});if(r.ok)location.reload();else alert('Pause update failed');}}</script></body></html>""")


@app.post("/api/candidates/{candidate_id}/decision")
async def candidate_decision(
    candidate_id: UUID,
    body: DecisionBody,
    request: Request,
    _user: str = Depends(operator_auth),
):
    same_origin(request)
    if body.action not in {"watch", "reject"}:
        raise HTTPException(422, "action must be watch or reject")
    async with app.state.db.acquire() as conn, conn.transaction():
        try:
            row = await Repository(conn).decide_candidate(
                candidate_id, body.action, body.note, app.state.settings.git_commit_sha
            )
        except InvalidCandidateTransition as exc:
            raise HTTPException(409, str(exc)) from exc
    if not row:
        raise HTTPException(404, "candidate not found")
    return api_value(dict(row))


@app.post("/api/operator/research-paused")
async def research_paused(
    body: PauseBody, request: Request, _user: str = Depends(operator_auth)
):
    same_origin(request)
    async with app.state.db.acquire() as conn, conn.transaction():
        paused = await Repository(conn).set_research_paused(body.research_paused)
    return {"research_paused": paused}
