from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from tradebot.market_watch import MarketWatchService
from tradebot.queue import ResearchQueue
from tradebot.repository import InvalidCandidateTransition, Repository


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class PersistenceConn:
    def __init__(self):
        self.candidate_id = uuid4()
        self.recent = None
        self.source_count = 0
        self.snapshot_count = 0
        self.json_values = []
        self.promoted = False

    def transaction(self):
        return Transaction()

    async def fetchval(self, query, *args):
        if "INSERT INTO strategy_versions" in query:
            self.json_values.append(args[3])
            return uuid4()
        if "INSERT INTO source_events" in query:
            self.source_count += 1
            self.json_values.append(args[1])
            return uuid4()
        if "INSERT INTO research_candidates" in query:
            self.json_values.extend(args[7:11])
            self.recent = {
                "id": self.candidate_id,
                "state": "rejected",
                "decision_id": None,
            }
            return self.candidate_id
        if "INSERT INTO market_snapshots" in query:
            self.snapshot_count += 1
            self.json_values.append(args[-1])
            return uuid4()
        raise AssertionError(query)

    async def fetchrow(self, query, *_args):
        if "FROM research_candidates rc" in query:
            return self.recent
        raise AssertionError(query)

    async def execute(self, query, *_args):
        if "UPDATE research_candidates SET state='eligible'" in query:
            self.promoted = True
            return "UPDATE 1"
        raise AssertionError(query)


class FakeDb:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def pair(liquidity):
    return {
        "pairAddress": "pair",
        "baseToken": {"address": "token", "symbol": "TOK"},
        "priceUsd": "1.25",
        "liquidity": {"usd": liquidity},
        "volume": {"h1": "250000", "h6": "600000", "h24": "6000000"},
        "priceChange": {"h1": "10", "h6": "20", "h24": "80"},
        "marketCap": "1000000",
    }


@pytest.mark.asyncio
async def test_jsonb_persistence_and_cooldown_preserve_later_eligible_observation():
    conn = PersistenceConn()
    service = MarketWatchService(FakeDb(conn), object(), "exact-sha")

    await service._persist_pair(pair("100"))
    await service._persist_pair(pair("500000"))

    assert conn.source_count == 2
    assert conn.snapshot_count == 2
    assert conn.promoted is True
    assert conn.json_values and all(
        isinstance(value, str) for value in conn.json_values
    )


class DecisionConn:
    def __init__(self, candidate, baseline=None):
        self.candidate = candidate
        self.baseline = baseline
        self.outcome_inserts = 0

    async def fetchrow(self, query, *_args):
        if "FROM research_candidates rc" in query:
            return self.candidate
        if "FROM market_snapshots" in query:
            return self.baseline
        if "INSERT INTO candidate_decisions" in query:
            return {
                "operator_action": "watch",
                "baseline_price_usd": self.baseline["price_usd"],
            }
        raise AssertionError(query)

    async def execute(self, query, *_args):
        if "INSERT INTO outcome_checks" in query:
            self.outcome_inserts += 1
        return "UPDATE 1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate",
    [
        {
            "id": uuid4(),
            "strategy_version_id": uuid4(),
            "state": "rejected",
            "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
            "candidate_rank": 1,
        },
        {
            "id": uuid4(),
            "strategy_version_id": uuid4(),
            "state": "eligible",
            "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
            "candidate_rank": 1,
        },
    ],
)
async def test_rejected_and_expired_candidates_are_not_actionable(candidate):
    with pytest.raises(InvalidCandidateTransition):
        await Repository(DecisionConn(candidate)).decide_candidate(
            candidate["id"], "watch", None, "exact-sha"
        )


@pytest.mark.asyncio
async def test_decision_captures_baseline_and_schedules_four_outcomes():
    candidate = {
        "id": uuid4(),
        "strategy_version_id": uuid4(),
        "state": "eligible",
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "candidate_rank": 1,
    }
    baseline = {"id": uuid4(), "price_usd": Decimal("1.25")}
    conn = DecisionConn(candidate, baseline)

    decision = await Repository(conn).decide_candidate(
        candidate["id"], "watch", None, "exact-sha"
    )

    assert decision["baseline_price_usd"] == Decimal("1.25")
    assert conn.outcome_inserts == 4


@pytest.mark.asyncio
async def test_completed_outcome_calculates_market_price_change_from_baseline():
    class QueueConn:
        async def execute(self, query, *_args):
            assert "market_price_change_pct" in query
            assert "baseline_price_usd > 0" in query

    await ResearchQueue(QueueConn()).complete_market_outcome(uuid4(), uuid4(), "1.50")
