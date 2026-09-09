import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _load_alembic_env_module(monkeypatch):
    from alembic import context

    monkeypatch.setattr(
        context,
        "config",
        SimpleNamespace(config_file_name=None),
        raising=False,
    )
    monkeypatch.setattr(context, "is_offline_mode", lambda: True, raising=False)
    monkeypatch.setattr(context, "configure", lambda **_kwargs: None, raising=False)
    monkeypatch.setattr(context, "run_migrations", lambda: None, raising=False)
    monkeypatch.setattr(
        context,
        "begin_transaction",
        lambda: _NoopTransaction(),
        raising=False,
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@example.com/db")

    path = Path("alembic/env.py")
    spec = importlib.util.spec_from_file_location("alembic_env_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _NoopTransaction:
    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return None


def test_alembic_normalizes_postgresql_url_to_psycopg_driver(monkeypatch):
    module = _load_alembic_env_module(monkeypatch)

    assert (
        module.normalize_sqlalchemy_postgres_url("postgresql://u:p@example.com/db")
        == "postgresql+psycopg://u:p@example.com/db"
    )


def test_alembic_normalizes_postgres_url_to_psycopg_driver(monkeypatch):
    module = _load_alembic_env_module(monkeypatch)

    assert (
        module.normalize_sqlalchemy_postgres_url("postgres://u:p@example.com/db")
        == "postgresql+psycopg://u:p@example.com/db"
    )


def test_alembic_leaves_existing_psycopg_url_unchanged(monkeypatch):
    module = _load_alembic_env_module(monkeypatch)
    url = "postgresql+psycopg://u:p@example.com/db"

    assert module.normalize_sqlalchemy_postgres_url(url) == url
