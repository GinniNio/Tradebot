from __future__ import annotations

import argparse
import asyncio
import json
import sys

from tradebot.config import ConfigError, load_settings
from tradebot.db import Database
from tradebot.health import build_health
from tradebot.scanner import ScannerState

EXPECTED_DOCTOR_ERRORS = (OSError, TimeoutError, RuntimeError)


def _budget_report(settings) -> dict:
    return {
        "dexscreener": settings.dexscreener.budget.__dict__,
        "helius": settings.helius.budget.__dict__,
        "jupiter": settings.jupiter.budget.__dict__,
        "rugcheck": settings.rugcheck.budget.__dict__,
    }


async def run_doctor() -> dict:
    settings = load_settings()
    db = Database(settings)
    await db.connect()
    try:
        health = await build_health(settings, db, ScannerState(status="doctor"))
    finally:
        await db.close()
    health["doctor"] = {
        "migration": health.get("database", {}).get("migration", {}),
        "lock_readiness": {
            "configured": bool(settings.scanner_lock_database_url),
            "connection": "not_acquired_by_doctor",
        },
        "providers": settings.providers_configured,
        "budgets": _budget_report(settings),
        "alert_configuration": {"status": "deferred_to_pr_e"},
        "research_execution_enabled": settings.research_execution_enabled,
    }
    return health


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate managed Tradebot foundation")
    parser.add_argument("--json", action="store_true", help="emit JSON output")
    parser.parse_args(argv)
    try:
        result = asyncio.run(run_doctor())
    except ConfigError as exc:
        print(f"configuration failed: {exc}", file=sys.stderr)
        return 2
    except EXPECTED_DOCTOR_ERRORS as exc:
        print(f"doctor failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, default=str, indent=2))
    return 0 if result.get("database", {}).get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
