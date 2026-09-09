from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from tradebot.config import Settings

KEEPALIVE_OPTIONS = {
    "keepalives": "1",
    "keepalives_idle": "15",
    "keepalives_interval": "5",
    "keepalives_count": "3",
}


def with_keepalives(dsn: str) -> str:
    parts = urlsplit(dsn)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(KEEPALIVE_OPTIONS)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class AdvisoryLock:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.conn: Any | None = None
        self.acquired = False
        self.last_error_category: str | None = None

    async def acquire(self) -> bool:
        import asyncpg

        self.conn = await asyncpg.connect(dsn=with_keepalives(self.settings.scanner_lock_database_url))
        self.acquired = bool(await self.conn.fetchval("SELECT pg_try_advisory_lock($1)", self.settings.scanner_lock_key))
        return self.acquired

    async def release(self) -> None:
        if self.conn:
            if self.acquired:
                await self.conn.execute("SELECT pg_advisory_unlock($1)", self.settings.scanner_lock_key)
            await self.conn.close()
        self.conn = None
        self.acquired = False
