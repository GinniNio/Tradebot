"""
server.py — Tradebot Web Dashboard Backend

FastAPI server that:
  - Serves the dashboard at http://localhost:3001
  - Provides REST endpoints for all bot data
  - Broadcasts live updates via WebSocket every 2 seconds
  - Exposes strategy management (add wallet, add zombie, create strategy)

Run:  python server.py
      (keep main.py running separately, or integrate via run_all.py)
"""

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── make sure we import from the Tradebot directory ──────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

from database import (
    init_db, get_smart_wallets, get_open_positions, get_pending_signals,
    get_zombie_watchlist, get_trade_stats, add_smart_wallet,
    add_to_zombie_watchlist, get_hot_tokens, create_signal,
    signal_outcomes_summary, get_signal_outcomes_list,
    get_pending_price_checks_list, get_ops_health,
)
from config import (
    PAPER_TRADING, TRADE_SIZE_SOL, MAX_OPEN_POSITIONS,
    STRATEGY_STATES, TRADABLE_STATES, WALLET_TRACKER_ENABLED,
    ZOMBIE_USE_BIRDEYE,
)

logger = logging.getLogger(__name__)


# ─── Lifespan: replaces deprecated @app.on_event("startup") ──────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(message)s")
    broadcaster = asyncio.create_task(broadcast_loop(), name="broadcast_loop")
    logger.info("Tradebot dashboard running at http://localhost:3001")
    try:
        yield
    finally:
        broadcaster.cancel()
        try:
            await broadcaster
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Tradebot Dashboard", version="1.0", lifespan=lifespan)

# ── Static files (dashboard HTML) ────────────────────────────────────────────
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ─── WebSocket connection manager ────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

manager = ConnectionManager()


# ─── Data helpers ────────────────────────────────────────────────────────────

def _compute_confidence(signal_type: str, trigger_count: int,
                         extra_data: str = None) -> int:
    """
    Heuristic confidence score 0–100 for display.
    Wallet signals: scaled by how many wallets triggered.
    Zombie signals: based on volume spike magnitude if available.
    """
    if signal_type == "wallet":
        from config import WALLET_TRIGGER_COUNT
        base = min(100, int((trigger_count / WALLET_TRIGGER_COUNT) * 60) + 40)
        return base
    elif signal_type == "zombie":
        if extra_data:
            try:
                extra = json.loads(extra_data)
                vol_1h = extra.get("volume_1h", 0) or 0
                chg    = extra.get("price_change_1h_pct", 0) or 0
                score  = min(100, int(40 + min(chg, 30) + min(vol_1h / 1000, 30)))
                return score
            except Exception:
                pass
        return 65
    return 50


def _build_snapshot() -> dict:
    """Assemble the full dashboard data snapshot."""
    positions  = get_open_positions()
    signals    = get_pending_signals()
    wallets    = get_smart_wallets()
    zombies    = get_zombie_watchlist()
    stats      = get_trade_stats()
    hot_tokens = get_hot_tokens(within_minutes=15, min_wallet_count=2)

    # Recent signals (last 20, acted or not)
    from database import get_conn
    with get_conn() as conn:
        recent_sigs = [
            dict(r) for r in conn.execute(
                "SELECT * FROM signals ORDER BY created_at DESC LIMIT 20"
            ).fetchall()
        ]

    # Recent trades
    with get_conn() as conn:
        recent_trades = [
            dict(r) for r in conn.execute(
                "SELECT * FROM trade_history ORDER BY closed_at DESC LIMIT 10"
            ).fetchall()
        ]

    # Enrich signals with confidence
    for s in recent_sigs:
        s["confidence"] = _compute_confidence(
            s["signal_type"], s["trigger_count"], s.get("extra_data")
        )

    total  = stats.get("total_trades") or 0
    wins   = stats.get("wins") or 0
    win_rate = round((wins / total * 100) if total > 0 else 0, 1)

    # Strategy states from config — exposed so the dashboard can show whether
    # each strategy is in RESEARCH/PAPER/etc. and whether it can open positions.
    strategy_states = {
        name: {"state": state, "can_trade": state in TRADABLE_STATES}
        for name, state in STRATEGY_STATES.items()
    }

    # Strategy health from signal_outcomes — the Phase 3 measurement layer.
    # Empty list until the first live signal fires and matures past 5m.
    try:
        strategy_health = signal_outcomes_summary()
    except Exception as e:
        logger.error("strategy_health query failed: %s", e)
        strategy_health = []

    return {
        "ts":            datetime.utcnow().isoformat(),
        "paper_trading": PAPER_TRADING,
        "strategy_states": strategy_states,
        "strategy_health": strategy_health,
        "strategies": {
            "wallet_tracker": {
                "active":          WALLET_TRACKER_ENABLED,
                "state":           STRATEGY_STATES.get("wallet_convergence", "RESEARCH"),
                "can_trade":       STRATEGY_STATES.get("wallet_convergence") in TRADABLE_STATES,
                "tracked_wallets": len(wallets),
                "hot_tokens":      len(hot_tokens),
            },
            "zombie_tracker": {
                "active":    True,
                "state":     STRATEGY_STATES.get("zombie_revival", "RESEARCH"),
                "can_trade": STRATEGY_STATES.get("zombie_revival") in TRADABLE_STATES,
                "watchlist": len(zombies),
                "triggered": len([z for z in zombies if z["status"] == "triggered"]),
            },
        },
        "positions":      positions,
        "signals":        recent_sigs,
        "hot_tokens":     hot_tokens,
        "recent_trades":  recent_trades,
        "wallets":        wallets[:50],     # cap for UI
        "zombies":        zombies[:50],
        "stats": {
            "total_trades":    total,
            "win_rate":        win_rate,
            "total_pnl_usd":   round(stats.get("total_pnl_usd") or 0, 2),
            "avg_pnl_pct":     round(stats.get("avg_pnl_pct") or 0, 1),
            "open_positions":  len(positions),
            "max_positions":   MAX_OPEN_POSITIONS,
            "trade_size_sol":  TRADE_SIZE_SOL,
        },
    }


# ─── HTTP routes ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_file = STATIC_DIR / "index.html"
    if html_file.exists():
        return HTMLResponse(html_file.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Dashboard not found — check static/index.html</h1>", status_code=404)

@app.get("/api/snapshot")
async def snapshot():
    return _build_snapshot()

@app.get("/api/stats")
async def stats():
    return get_trade_stats()

@app.get("/api/strategy_health")
async def strategy_health(source: str = "live"):
    """Phase 3 verdict query: per-strategy signal count, route rate, and
    average post-signal returns at 5m/15m/1h/24h. NULL averages mean no
    checks have completed at that horizon yet."""
    return signal_outcomes_summary(source=source)


@app.get("/api/ops_health")
async def ops_health():
    """Compact health snapshot for the dashboard top panel.
    Includes counts + a couple of derived timestamps. Read-only."""
    payload = get_ops_health()
    payload.update({
        "mode": "PAPER" if PAPER_TRADING else "LIVE",
        "wallet_tracker_enabled": WALLET_TRACKER_ENABLED,
        "zombie_use_birdeye": ZOMBIE_USE_BIRDEYE,
        "tradable_strategies": sum(
            1 for s in STRATEGY_STATES.values() if s in TRADABLE_STATES
        ),
    })
    return payload


@app.get("/api/signal_outcomes")
async def signal_outcomes(source: str = "live", limit: int = 50):
    """List signal_outcomes rows. source: live|backfill|all. limit max 500."""
    if source == "all":
        source_arg = None
    elif source in ("live", "backfill"):
        source_arg = source
    else:
        raise HTTPException(status_code=400, detail="source must be live|backfill|all")
    return get_signal_outcomes_list(source=source_arg, limit=min(max(limit, 1), 500))


@app.get("/api/pending_price_checks")
async def pending_price_checks(limit: int = 100, overdue_only: bool = False):
    """List pending_price_checks rows ordered by due_at. overdue_only=True
    filters to checks that are >10 minutes past due."""
    return get_pending_price_checks_list(
        limit=min(max(limit, 1), 500),
        only_pending=True,
        overdue_minutes=10 if overdue_only else None,
    )

@app.get("/api/positions")
async def positions():
    return get_open_positions()

@app.get("/api/signals")
async def signals():
    from database import get_conn
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM signals ORDER BY created_at DESC LIMIT 30"
        ).fetchall()]
    for r in rows:
        r["confidence"] = _compute_confidence(r["signal_type"], r["trigger_count"], r.get("extra_data"))
    return rows

@app.get("/api/wallets")
async def wallets():
    return get_smart_wallets()

@app.get("/api/zombies")
async def zombies():
    return get_zombie_watchlist()


# ─── Pydantic request models ─────────────────────────────────────────────────

class AddWalletRequest(BaseModel):
    address: str
    chain: str = "solana"
    win_rate: float = 0.0
    notes: str = ""

class AddZombieRequest(BaseModel):
    chain: str
    token_address: str
    token_symbol: str = ""
    name: str = ""
    peak_volume_usd: float = 0.0

class CreateStrategyRequest(BaseModel):
    description: str          # plain-English strategy description
    strategy_type: str = "wallet"  # 'wallet' or 'zombie'


# ─── Mutation routes ─────────────────────────────────────────────────────────

@app.post("/api/wallets/add")
async def add_wallet(req: AddWalletRequest):
    from database import get_smart_wallets
    existing = {w["address"] for w in get_smart_wallets()}
    if req.address in existing:
        raise HTTPException(status_code=409, detail="Wallet already tracked")
    added = add_smart_wallet(req.address, req.chain, req.win_rate, notes=req.notes)
    if not added:
        raise HTTPException(status_code=500, detail="Failed to add wallet")
    await manager.broadcast({"event": "wallet_added", "address": req.address})
    return {"status": "ok", "address": req.address}

@app.post("/api/zombies/add")
async def add_zombie(req: AddZombieRequest):
    added = add_to_zombie_watchlist(
        req.chain, req.token_address, req.token_symbol,
        req.name, req.peak_volume_usd
    )
    if not added:
        raise HTTPException(status_code=409, detail="Token already on watchlist")
    await manager.broadcast({"event": "zombie_added", "address": req.token_address})
    return {"status": "ok", "token_address": req.token_address}

@app.post("/api/strategy/create")
async def create_strategy(req: CreateStrategyRequest):
    """
    Natural language strategy parser.
    Extracts parameters from plain English descriptions.
    Currently supports wallet and zombie strategy types.
    """
    desc   = req.description.lower()
    result = {"strategy_type": req.strategy_type, "description": req.description, "params": {}}

    # ── Wallet strategy parsing ────────────────────────────────────────────
    if req.strategy_type == "wallet":
        import re

        # Extract wallet count threshold
        m = re.search(r'(\d+)\s*(or more|\\+)?\s*(smart\s*)?(wallet|trader|address)', desc)
        if m:
            result["params"]["wallet_trigger_count"] = int(m.group(1))

        # Extract time window
        time_map = {"minute": 1, "min": 1, "hour": 60, "hr": 60, "day": 1440}
        m_time = re.search(r'(\d+)\s*(minute|min|hour|hr|day)', desc)
        if m_time:
            multiplier = time_map.get(m_time.group(2), 1)
            result["params"]["wallet_lookback_minutes"] = int(m_time.group(1)) * multiplier

        # Extract mcap ceiling
        m_mcap = re.search(r'under\s*\$?([\d,]+)\s*k?\s*(mcap|market cap)?', desc)
        if m_mcap:
            raw = m_mcap.group(1).replace(",", "")
            multiplier = 1000 if "k" in desc[m_mcap.start():m_mcap.end()+1] else 1
            result["params"]["max_mcap_usd"] = int(raw) * multiplier

    # ── Zombie strategy parsing ────────────────────────────────────────────
    elif req.strategy_type == "zombie":
        import re

        m_dormant = re.search(r'(\d+)\s*(day|week)', desc)
        if m_dormant:
            days = int(m_dormant.group(1))
            if "week" in m_dormant.group(2):
                days *= 7
            result["params"]["dormant_days"] = days

        m_vol = re.search(r'(\d+)\s*[x×]\s*(volume|vol)', desc)
        if m_vol:
            result["params"]["volume_multiplier"] = float(m_vol.group(1))

        m_pct = re.search(r'(\d+)\s*%?\s*(price|pump|rise|up)', desc)
        if m_pct:
            result["params"]["min_price_change_pct"] = float(m_pct.group(1))

    result["created_at"] = datetime.utcnow().isoformat()
    result["message"] = f"Strategy parsed. Apply these params in config.py to activate."
    await manager.broadcast({"event": "strategy_created", "result": result})
    return result


# ─── WebSocket live feed ─────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    logger.info("WebSocket client connected")
    try:
        # Send immediate snapshot on connect
        await ws.send_json(_build_snapshot())
        while True:
            # Keep alive + check for client pings
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(ws)
        logger.info("WebSocket client disconnected")


# ─── Background broadcaster ───────────────────────────────────────────────────

async def broadcast_loop():
    """Push dashboard snapshots to all connected clients every 3 seconds."""
    while True:
        await asyncio.sleep(3)
        if manager.active:
            try:
                data = _build_snapshot()
                await manager.broadcast(data)
            except Exception as e:
                logger.error("Broadcast error: %s", e)


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=3001,
        reload=False,
        log_level="info",
    )
