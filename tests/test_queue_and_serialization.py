import asyncio
from decimal import Decimal

import pytest

from tradebot.queue import CLAIM_DUE_OUTCOME_CHECKS_SQL, ResearchQueue
from tradebot.repository import Repository
from tradebot.serialization import api_value


def test_due_work_claim_uses_postgres_skip_locked():
    sql = " ".join(CLAIM_DUE_OUTCOME_CHECKS_SQL.split())

    assert "status = 'pending' AND due_at <= now()" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "LIMIT $1" in sql


def test_base_unit_decimals_serialize_as_strings():
    payload = api_value({"amount": Decimal(1000000000000000000), "items": [Decimal(1)]})

    assert payload == {"amount": "1000000000000000000", "items": ["1"]}


def test_queue_rejects_non_positive_claim_limit():
    queue = ResearchQueue(conn=None)

    with pytest.raises(ValueError, match="limit must be positive"):
        asyncio.run(queue.claim_due_outcome_checks(0))


def test_quote_snapshot_write_uses_idempotency_key_and_strategy_version():
    names = Repository.create_quote_snapshot.__code__.co_varnames

    assert "idempotency_key" in names
    assert "strategy_version_id" in names
