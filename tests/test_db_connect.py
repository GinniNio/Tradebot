import asyncio
import sys
import types
from unittest.mock import AsyncMock

import pytest
from asyncpg.exceptions import CannotConnectNowError, InvalidPasswordError

from tests.test_config import valid_env
from tradebot import db as db_module
from tradebot.config import load_settings
from tradebot.db import Database


def _install(monkeypatch, create_pool):
    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(create_pool=create_pool))
    monkeypatch.setattr(db_module.asyncio, "sleep", AsyncMock())


def test_connect_retries_transient_errors_then_succeeds(monkeypatch):
    pool = object()
    create_pool = AsyncMock(
        side_effect=[ConnectionRefusedError(), CannotConnectNowError("waking"), pool]
    )
    _install(monkeypatch, create_pool)
    database = Database(load_settings(valid_env()))

    asyncio.run(database.connect())

    assert database.pool is pool
    assert create_pool.await_count == 3


def test_connect_gives_up_after_retry_budget(monkeypatch):
    create_pool = AsyncMock(side_effect=ConnectionRefusedError())
    _install(monkeypatch, create_pool)
    database = Database(load_settings(valid_env()))

    with pytest.raises(ConnectionRefusedError):
        asyncio.run(database.connect())

    assert create_pool.await_count == len(db_module.CONNECT_RETRY_DELAYS_SECONDS) + 1


def test_connect_does_not_retry_auth_errors(monkeypatch):
    create_pool = AsyncMock(side_effect=InvalidPasswordError("bad password"))
    _install(monkeypatch, create_pool)
    database = Database(load_settings(valid_env()))

    with pytest.raises(InvalidPasswordError):
        asyncio.run(database.connect())

    assert create_pool.await_count == 1
