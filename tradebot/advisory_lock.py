from __future__ import annotations

from inspect import isawaitable
from typing import Any

from tradebot.config import Settings

try:
    from asyncpg.exceptions import InterfaceError, PostgresError
except ImportError:  # pragma: no cover - asyncpg is installed in managed deployments.
    InterfaceError = PostgresError = ()

# Server-side TCP keepalives, so Postgres notices a dead scanner connection
# and releases its advisory lock. They are applied with SET after connecting:
# DSN query parameters become startup parameters, which Neon's proxy rejects
# (it reported InvalidPasswordError for tcp_keepalives_*), and the libpq
# "keepalives*" options are rejected by standard Postgres.
KEEPALIVE_SETTINGS = (
    ("tcp_keepalives_idle", "15"),
    ("tcp_keepalives_interval", "5"),
    ("tcp_keepalives_count", "3"),
)


async def apply_keepalives(conn: Any) -> None:
    """Best effort: a backend that refuses these must not block the lock."""
    for name, value in KEEPALIVE_SETTINGS:
        try:
            await conn.execute(f"SET {name} = {value}")
        except (InterfaceError, PostgresError):
            return


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

        self.conn = await asyncpg.connect(dsn=self.settings.scanner_lock_database_url)
        await apply_keepalives(self.conn)
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
