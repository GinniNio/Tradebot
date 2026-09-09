from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from tradebot.config import Settings


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool: Any | None = None

    async def connect(self) -> None:
        import asyncpg

        self.pool = await asyncpg.create_pool(dsn=self.settings.database_url, min_size=1, max_size=5)

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
