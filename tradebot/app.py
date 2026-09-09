from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from tradebot.advisory_lock import AdvisoryLock
from tradebot.config import Settings, load_settings
from tradebot.db import Database
from tradebot.health import build_health
from tradebot.scanner import ManagedScanner, ScannerState


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
        scanner = ManagedScanner(scanner_state)
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


app = FastAPI(title="Tradebot Managed Research Service", version="0.1.0", lifespan=lifespan)


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
