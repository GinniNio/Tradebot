from __future__ import annotations

from tradebot.config import Settings
from tradebot.db import Database, migration_status
from tradebot.scanner import ScannerState

EXPECTED_HEALTH_ERRORS = (OSError, TimeoutError, RuntimeError)


async def build_health(settings: Settings, db: Database | None, scanner: ScannerState) -> dict:
    database = {"ok": False, "migration": {"ok": False, "revision": None}}
    queue = {"overdue_checks": None, "failed_checks": None}
    operator = {"research_paused": None}
    if db and db.pool:
        try:
            async with db.acquire() as conn:
                await conn.fetchval("SELECT 1")
                database = {"ok": True, "migration": await migration_status(conn)}
                counts = await conn.fetchrow(
                    """
                    SELECT
                        COUNT(*) FILTER (
                            WHERE status = 'pending' AND due_at < now()
                        ) AS overdue_checks,
                        COUNT(*) FILTER (WHERE status = 'failed') AS failed_checks
                    FROM outcome_checks
                    """
                )
                queue = dict(counts) if counts else queue
                research_paused = await conn.fetchval("SELECT research_paused FROM operator_settings WHERE id = true")
                operator = {"research_paused": research_paused}
        except EXPECTED_HEALTH_ERRORS as exc:
            database = {"ok": False, "error": type(exc).__name__}
    return {
        "status": "ok" if database.get("ok") else "degraded",
        "build": {"git_commit_sha": settings.git_commit_sha, "app_env": settings.app_env},
        "database": database,
        "scanner": {
            "state": scanner.status,
            "cycles_completed": scanner.cycles_completed,
            "last_cycle_error": scanner.last_cycle_error,
        },
        "providers": settings.providers_configured,
        "queue": queue,
        "operator": operator,
    }
