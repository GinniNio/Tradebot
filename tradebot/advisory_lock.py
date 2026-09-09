from __future__ import annotations

from inspect import isawaitable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from tradebot.config import Settings

try:
    from asyncpg.exceptions import InterfaceError, PostgresError
except ImportError:  # pragma: no cover - asyncpg is installed in managed deployments.
    InterfaceError = PostgresError = ()

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


def _conn_is_closed(conn: Any) -> bool:
    is_closed = getattr(conn, "is_closed", None)
    if callable(is_closed):
        result = is_closed()
        if isawaitable(result):
            close = getattr(result, "close", None)
            if callable(close):
                close()
            return False
        return bool(result)
    return False


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
        if self.conn and not _conn_is_closed(self.conn):
            if self.acquired:
                try:
                    await self.conn.execute("SELECT pg_advisory_unlock($1)", self.settings.scanner_lock_key)
                except (OSError, TimeoutError, RuntimeError, InterfaceError, PostgresError) as exc:
                    self.last_error_category = type(exc).__name__
            try:
                await self.conn.close()
            except (OSError, TimeoutError, RuntimeError, InterfaceError, PostgresError) as exc:
                self.last_error_category = type(exc).__name__
        self.conn = None
        self.acquired = False
