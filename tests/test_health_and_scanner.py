import asyncio
from contextlib import asynccontextmanager

from asyncpg.exceptions import UndefinedTableError

from tests.test_config import valid_env
from tradebot.app import app, healthz
from tradebot.config import load_settings
from tradebot.health import build_health
from tradebot.scanner import ManagedScanner, ScannerState


def test_health_without_database_is_degraded():
    health = asyncio.run(build_health(load_settings(valid_env()), None, ScannerState(status="standby")))

    assert health["status"] == "degraded"
    assert health["scanner"]["state"] == "standby"
    assert health["providers"]["rugcheck"] == "unavailable"
    assert health["operator"]["research_paused"] is None


def test_health_missing_outcome_checks_returns_degraded_json():
    class MissingTableConn:
        async def fetchval(self, _query):
            return 1

        async def fetchrow(self, _query):
            raise UndefinedTableError("relation outcome_checks does not exist")

    class MissingTableDb:
        pool = object()

        @asynccontextmanager
        async def acquire(self):
            yield MissingTableConn()

    health = asyncio.run(
        build_health(load_settings(valid_env()), MissingTableDb(), ScannerState(status="active"))
    )

    assert health["status"] == "degraded"
    assert health["database"]["ok"] is False
    assert health["database"]["error"] == "UndefinedTableError"
    assert "relation" not in str(health)


def test_health_endpoint_returns_503_for_degraded_database():
    class MissingTableConn:
        async def fetchval(self, _query):
            return 1

        async def fetchrow(self, _query):
            raise UndefinedTableError("relation outcome_checks does not exist")

    class MissingTableDb:
        pool = object()

        @asynccontextmanager
        async def acquire(self):
            yield MissingTableConn()

    app.state.settings = load_settings(valid_env())
    app.state.db = MissingTableDb()
    app.state.scanner_state = ScannerState(status="active")

    response = asyncio.run(healthz())

    assert response.status_code == 503


def test_scanner_shutdown_cancels_in_flight_task():
    async def run_case():
        state = ScannerState(status="active")
        scanner = ManagedScanner(state)
        task = asyncio.create_task(asyncio.sleep(60))
        state.in_flight.add(task)

        await scanner.shutdown(timeout_seconds=0.01)

        assert task.cancelled()
        assert state.status == "stopped"

    asyncio.run(run_case())
