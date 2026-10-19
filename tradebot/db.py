from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from tradebot.config import Settings

try:
    from asyncpg.exceptions import (
        CannotConnectNowError,
        PostgresConnectionError,
        TooManyConnectionsError,
    )
except ImportError:  # pragma: no cover - asyncpg is installed in managed deployments.
    CannotConnectNowError = PostgresConnectionError = TooManyConnectionsError = ()

logger = logging.getLogger(__name__)

# Transient failures (Neon compute waking, network blips, connection limits) are
# retried so one bad moment at boot does not exit uvicorn with status 3.
# Auth and config errors are not retried: they fail fast.
TRANSIENT_CONNECT_ERRORS = (
    OSError,
    TimeoutError,
    CannotConnectNowError,
    PostgresConnectionError,
    TooManyConnectionsError,
)
CONNECT_RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0, 8.0)


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool: Any | None = None

    async def connect(self) -> None:
        import asyncpg

        for attempt, delay in enumerate((*CONNECT_RETRY_DELAYS_SECONDS, None), start=1):
            try:
                self.pool = await asyncpg.create_pool(
                    dsn=self.settings.database_url, min_size=1, max_size=5
                )
                return
            except TRANSIENT_CONNECT_ERRORS as exc:
                if delay is None:
                    raise
                logger.warning(
                    "database connect attempt %d failed (%s); retrying in %.0fs",
                    attempt,
                    type(exc).__name__,
                    delay,
                )
                await asyncio.sleep(delay)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        if not self.pool:
            raise RuntimeError("database pool is not connected")
        async with self.pool.acquire() as conn:
            yield conn


async def migration_status(conn: Any) -> dict[str, str | bool | None]:
    exists = await conn.fetchval("SELECT to_regclass('public.alembic_version') IS NOT NULL")
    if not exists:
        return {"ok": False, "revision": None}
    revision = await conn.fetchval("SELECT version_num FROM alembic_version LIMIT 1")
    return {"ok": bool(revision), "revision": revision}
