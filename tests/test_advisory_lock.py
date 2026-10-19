import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock

from tests.test_config import valid_env
from tradebot.advisory_lock import AdvisoryLock
from tradebot.config import load_settings


def test_lock_connects_with_configured_dsn_and_sets_keepalives(monkeypatch):
    conn = AsyncMock()
    conn.fetchval.return_value = True
    connect = AsyncMock(return_value=conn)
    _install_asyncpg_stub(monkeypatch, connect)
    settings = load_settings(valid_env())
    lock = AdvisoryLock(settings)

    assert asyncio.run(lock.acquire()) is True

    # The DSN is passed through untouched: no startup parameters are added.
    assert connect.await_args.kwargs["dsn"] == settings.scanner_lock_database_url
    statements = [call.args[0] for call in conn.execute.await_args_list]
    assert statements == [
        "SET tcp_keepalives_idle = 15",
        "SET tcp_keepalives_interval = 5",
        "SET tcp_keepalives_count = 3",
    ]


def test_refused_keepalive_setting_does_not_block_the_lock(monkeypatch):
    from asyncpg.exceptions import InsufficientPrivilegeError

    conn = AsyncMock()
    conn.fetchval.return_value = True
    conn.execute.side_effect = InsufficientPrivilegeError("not allowed")
    _install_asyncpg_stub(monkeypatch, AsyncMock(return_value=conn))

    assert asyncio.run(AdvisoryLock(load_settings(valid_env())).acquire()) is True


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
    assert not any("pg_advisory_unlock" in call.args[0] for call in conn.execute.await_args_list)
    conn.close.assert_awaited_once()


def test_advisory_lock_releases_when_acquired(monkeypatch):
    conn = AsyncMock()
    conn.fetchval.return_value = True
    _install_asyncpg_stub(monkeypatch, AsyncMock(return_value=conn))
    lock = AdvisoryLock(load_settings(valid_env()))

    assert asyncio.run(lock.acquire()) is True
    asyncio.run(lock.release())

    assert "pg_advisory_unlock" in conn.execute.await_args.args[0]


def test_advisory_lock_release_is_idempotent_when_connection_already_closed():
    conn = AsyncMock()
    conn.is_closed = MagicMock(return_value=True)
    lock = AdvisoryLock(load_settings(valid_env()))
    lock.conn = conn
    lock.acquired = True

    asyncio.run(lock.release())

    conn.execute.assert_not_called()
    conn.close.assert_not_called()
    assert lock.conn is None
    assert lock.acquired is False


def test_advisory_lock_release_masks_unlock_and_close_errors():
    conn = AsyncMock()
    conn.is_closed = MagicMock(return_value=False)
    conn.execute.side_effect = RuntimeError("network details")
    conn.close.side_effect = RuntimeError("close details")
    lock = AdvisoryLock(load_settings(valid_env()))
    lock.conn = conn
    lock.acquired = True

    asyncio.run(lock.release())

    assert lock.conn is None
    assert lock.acquired is False
    assert lock.last_error_category == "RuntimeError"
