from __future__ import annotations

import html
import secrets
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from tradebot.advisory_lock import AdvisoryLock
from tradebot.candidate_rendering import render_rejected_audit_rows
from tradebot.config import Settings, load_settings
from tradebot.db import Database
from tradebot.dexscreener import DexscreenerClient
from tradebot.health import build_health
from tradebot.market_watch import MarketWatchService
from tradebot.repository import InvalidCandidateTransition, Repository
from tradebot.scanner import ManagedScanner, ScannerState
from tradebot.serialization import api_value


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    db = Database(settings)
    await db.connect()
    scanner_state = ScannerState()
    lock = AdvisoryLock(settings)
    scanner: ManagedScanner | None = None
    try:
        acquired = await lock.acquire()
    except (OSError, TimeoutError, RuntimeError) as exc:
        lock.last_error_category = type(exc).__name__
        acquired = False
    if acquired:
        market_watch = MarketWatchService(
            db, DexscreenerClient(settings.dexscreener.budget), settings.git_commit_sha
        )
        scanner = ManagedScanner(
            scanner_state, market_watch.run_cycle, settings.scanner_interval_seconds
        )
        scanner.start()
    else:
        scanner_state.status = "standby"
        if lock.last_error_category:
            scanner_state.last_cycle_error = f"lock_error:{lock.last_error_category}"
        await lock.release()
    app.state.settings = settings
    app.state.db = db
    app.state.scanner_state = scanner_state
    app.state.scanner_lock = lock
    app.state.scanner = scanner
    try:
        yield
    finally:
        try:
            if scanner:
                await scanner.shutdown(timeout_seconds=5.0)
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
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
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
    return {"service": "tradebot-managed", "health": "/healthz"}


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
    <body><h1>Daily Market Watch</h1><p>Market-screening research only. Not a trade recommendation or executable return.</p>
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
