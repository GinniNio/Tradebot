import asyncio
import sys
import types
from unittest.mock import AsyncMock

from tests.test_config import valid_env
from tradebot.advisory_lock import AdvisoryLock, with_keepalives
from tradebot.config import load_settings


def test_lock_dsn_adds_required_keepalives():
    dsn = with_keepalives("postgresql://u:p@example.com/db?sslmode=require")

    assert "sslmode=require" in dsn
    assert "keepalives=1" in dsn
    assert "keepalives_idle=15" in dsn
    assert "keepalives_interval=5" in dsn
    assert "keepalives_count=3" in dsn


def _install_asyncpg_stub(monkeypatch, connect):
    stub = types.SimpleNamespace(connect=connect)
    monkeypatch.setitem(sys.modules, "asyncpg", stub)


def test_advisory_lock_reports_standby_when_not_acquired(monkeypatch):
    conn = AsyncMock()
    conn.fetchval.return_value = False
    connect = AsyncMock(return_value=conn)
    _install_asyncpg_stub(monkeypatch, connect)
    lock = AdvisoryLock(load_settings(valid_env()))

    acquired = asyncio.run(lock.acquire())
    asyncio.run(lock.release())

    assert acquired is False
    assert lock.acquired is False
    conn.execute.assert_not_called()
    conn.close.assert_awaited_once()


def test_advisory_lock_releases_when_acquired(monkeypatch):
    conn = AsyncMock()
    conn.fetchval.return_value = True
    _install_asyncpg_stub(monkeypatch, AsyncMock(return_value=conn))
    lock = AdvisoryLock(load_settings(valid_env()))

    assert asyncio.run(lock.acquire()) is True
    asyncio.run(lock.release())

    conn.execute.assert_awaited_once()
    assert "pg_advisory_unlock" in conn.execute.await_args.args[0]
