"""End-to-end experiment rules against a real Postgres at Alembic head.

Run with TRADEBOT_TEST_DATABASE_URL pointing at a disposable database that
`alembic upgrade head` has been applied to. The test truncates its tables.
"""

import json
import os
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
import pytest_asyncio

from tradebot import experiment
from tradebot.market_watch import MarketWatchService, new_stats
from tradebot.verdict import OUTCOMES_SQL, collect_from_conn, summarize_outcomes

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

URL = os.environ.get("TRADEBOT_TEST_DATABASE_URL", "")


def _token(control: bool, prefix: str) -> str:
    for i in range(1000):
        token = f"{prefix}{i}"
        if experiment.in_control_sample(token) is control:
            return token
    raise AssertionError("no token found")


def pair(token, pair_address, *, liquidity, volume_24h="6000000", change_24h="80", price="1.0"):
    return {
        "pairAddress": pair_address,
        "baseToken": {"address": token, "symbol": token[:4]},
        "priceUsd": price,
        "liquidity": {"usd": liquidity},
        "volume": {"h1": "1000", "h6": "6000", "h24": volume_24h},
        "priceChange": {"h1": "5", "h6": "10", "h24": change_24h},
        "marketCap": "1000000",
    }


def eligible(token, pair_address, price="1.0"):
    p = pair(token, pair_address, liquidity="500000", price=price)
    p["volume"] = {"h1": "250000", "h6": "600000", "h24": "6000000"}
    return p


def low_score(token, pair_address, price="1.0"):
    # Passes all five hard filters, scores well under 65.
    return pair(token, pair_address, liquidity="60000", volume_24h="150000", change_24h="10", price=price)


class FakeClient:
    def __init__(self):
        self.profiles = []
        self.pairs = {}
        self.batch = {}
        self.sol = Decimal(100)

    async def discover(self):
        return [{"tokenAddress": t} for t in self.profiles]

    async def pairs_for_token(self, token):
        return [self.pairs[token]] if token in self.pairs else []

    async def pairs_batch(self, addresses):
        return {a: self.batch[a] for a in addresses if a in self.batch}

    async def sol_price_usd(self, _mint):
        return self.sol


class Db:
    def __init__(self, pool):
        self.pool = pool

    @asynccontextmanager
    async def acquire(self):
        async with self.pool.acquire() as conn:
            yield conn


@pytest_asyncio.fixture
async def db():
    if not URL:
        pytest.skip("TRADEBOT_TEST_DATABASE_URL not set")
    import asyncpg

    pool = await asyncpg.create_pool(dsn=URL, min_size=1, max_size=3)
    async with pool.acquire() as conn:
        await conn.execute(
            """TRUNCATE outcome_checks, candidate_decisions, market_snapshots,
            research_candidates, source_events, strategy_versions, scan_cycles CASCADE"""
        )
        await conn.execute("UPDATE operator_settings SET research_paused=false")
    yield Db(pool)
    await pool.close()


async def checks(db, token=None):
    async with db.acquire() as conn:
        return await conn.fetch(
            """SELECT * FROM outcome_checks WHERE ($1::text IS NULL OR token_address=$1)
            ORDER BY token_address, cohort, scheduled_at""",
            token,
        )


async def backdate(db, token, kind, *, scheduled_minutes_ago, status=None, claimed_minutes_ago=None):
    async with db.acquire() as conn:
        await conn.execute(
            """UPDATE outcome_checks SET scheduled_at=now() - make_interval(mins => $3),
            due_at=now() - make_interval(mins => $3),
            status=COALESCE($4, status),
            claimed_at=CASE WHEN $5::int IS NULL THEN claimed_at ELSE now() - make_interval(mins => $5) END
            WHERE token_address=$1 AND check_kind=$2""",
            token,
            kind,
            scheduled_minutes_ago,
            status,
            claimed_minutes_ago,
        )


async def test_experiment_rules_end_to_end(db):
    client = FakeClient()
    service = MarketWatchService(db, client, "test-sha")
    ctrl = _token(True, "ctrl")
    elig = _token(False, "elig")
    dead = _token(False, "dead")
    client.profiles = [ctrl, elig, dead]
    client.pairs = {
        ctrl: low_score(ctrl, "p-ctrl"),
        elig: eligible(elig, "p-elig"),
        dead: pair(dead, "p-dead", liquidity="1000"),
    }

    await service.run_cycle()

    rows = await checks(db)
    assert {(r["token_address"], r["cohort"]) for r in rows} == {(ctrl, "control"), (elig, "eligible")}
    assert len(rows) == 8
    assert all(r["scheduled_at"] == r["due_at"] for r in rows)
    assert all(r["sol_baseline_price_usd"] == Decimal(100) for r in rows)
    assert {r["trigger_source"] for r in rows} == {"profile_feed"}
    async with db.acquire() as conn:
        cycle = await conn.fetchrow("SELECT * FROM scan_cycles")
    assert cycle["status"] == "ok"
    cycle_stats = json.loads(cycle["stats_json"])
    assert (cycle_stats["eligible_tokens_started"], cycle_stats["control_tokens_started"]) == (1, 1)
    assert cycle_stats["rejection_reasons"]["Liquidity is below USD 50,000"] == 1

    # Later scan: the control token first qualifies at a new price. It gets an
    # eligible record from this moment; its control record is untouched.
    client.pairs[ctrl] = eligible(ctrl, "p-ctrl", price="2.0")
    client.pairs[elig] = eligible(elig, "p-elig", price="9.0")
    await service.run_cycle()
    rows = await checks(db, ctrl)
    by_cohort = {(r["cohort"], r["check_kind"]): r for r in rows}
    assert by_cohort[("control", "24h")]["baseline_price_usd"] == Decimal("1.0")
    assert by_cohort[("eligible", "24h")]["baseline_price_usd"] == Decimal("2.0")
    # A repeat observation of an already-measured token adds nothing.
    assert len(await checks(db, elig)) == 4
    assert {r["baseline_price_usd"] for r in await checks(db, elig)} == {Decimal("1.0")}

    # Observed price inside the window completes; a missing pair retries.
    await backdate(db, elig, "1h", scheduled_minutes_ago=5)
    await backdate(db, ctrl, "1h", scheduled_minutes_ago=5)
    client.batch = {"p-elig": eligible(elig, "p-elig", price="1.5")}
    client.sol = Decimal(110)
    await service.process_due_outcomes(new_stats())
    e1h = next(r for r in await checks(db, elig) if r["check_kind"] == "1h")
    assert e1h["status"] == "completed" and e1h["resolution"] == "observed"
    assert e1h["market_price_change_pct"] == Decimal(50)
    assert e1h["sol_outcome_price_usd"] == Decimal(110)
    c1h = [r for r in await checks(db, ctrl) if r["check_kind"] == "1h"]
    assert {r["status"] for r in c1h} == {"retryable"}
    assert all(r["scheduled_at"] < r["due_at"] for r in c1h)  # schedule kept, retry moved

    # Past the 15-minute tolerance with no price: unresolved, and no price is invented.
    await backdate(db, ctrl, "1h", scheduled_minutes_ago=14)
    await service.process_due_outcomes(new_stats())
    c1h = [r for r in await checks(db, ctrl) if r["check_kind"] == "1h"]
    assert {(r["status"], r["resolution"]) for r in c1h} == {("unresolved", "pair_missing")}
    assert all(r["outcome_price_usd"] is None and r["market_price_change_pct"] is None for r in c1h)

    # A claim abandoned by a restart is recovered; a fresh claim is left alone.
    await backdate(db, elig, "24h", scheduled_minutes_ago=1, status="claimed", claimed_minutes_ago=30)
    await backdate(db, elig, "3d", scheduled_minutes_ago=1, status="claimed", claimed_minutes_ago=1)
    client.batch = {"p-elig": eligible(elig, "p-elig", price="0.5")}
    await service.process_due_outcomes(new_stats())
    status = {r["check_kind"]: r["status"] for r in await checks(db, elig)}
    assert status["24h"] == "completed"
    assert status["3d"] == "claimed"

    # Picked up after its window (bot was down): closed without a price.
    await backdate(db, elig, "7d", scheduled_minutes_ago=24 * 60)
    await service.process_due_outcomes(new_stats())
    e7d = next(r for r in await checks(db, elig) if r["check_kind"] == "7d")
    assert (e7d["status"], e7d["resolution"]) == ("unresolved", "missed_window")
    assert e7d["outcome_price_usd"] is None

    # Report: unresolved counted separately and in the conservative scenario.
    async with db.acquire() as conn:
        report = summarize_outcomes(list(await conn.fetch(OUTCOMES_SQL, "market_watch_v2")))
    assert report["eligible/1h"]["observed"]["median"] == 50.0
    assert report["control/1h"]["unresolved"] == {"pair_missing": 1}
    assert report["control/1h"]["conservative_unresolved_as_minus_100"]["median"] == -100.0
    assert report["eligible/24h"]["primary"] is True
    # Control history kept and the overlap identified through the real report query.
    async with db.acquire() as conn:
        full = await collect_from_conn(conn, 7)
    assert full["overlap"]["tokens_in_both"] == 1 and full["overlap"]["tokens"] == [ctrl]
    assert full["enrolment"]["control"]["total"] == 1
    assert full["enrolment"]["eligible"] == {"total": 2, "profile_feed": 2}
    # An entry recorded before trigger_source existed reads as "unknown", never inferred.
    async with db.acquire() as conn:
        await conn.execute("UPDATE outcome_checks SET trigger_source = NULL WHERE token_address = $1", elig)
        legacy = await collect_from_conn(conn, 7)
    assert legacy["enrolment"]["eligible"] == {"total": 2, "profile_feed": 1, "unknown": 1}
    status = legacy["outcomes"]["eligible/24h"]["status"]
    assert sum(status.values()) == legacy["outcomes"]["eligible/24h"]["tokens"]


async def test_recheck_revisits_stored_tokens_and_stores_only_hard_passes(db):
    client = FakeClient()
    service = MarketWatchService(db, client, "test-sha")
    old_ok, old_bad, young = "old-ok", "old-bad", "young"
    async with db.acquire() as conn:
        version = await service._strategy_id(conn)
        for token, days in ((old_ok, 2), (old_bad, 3), (young, 0)):
            await conn.execute(
                """INSERT INTO research_candidates(idempotency_key,token_address,chain,selected_pair,
                strategy_version_id,state,created_at,expires_at)
                VALUES($1,$1,'solana',$2,$3,'rejected',now() - make_interval(days => $4),now())""",
                token,
                f"p-{token}",
                version,
                days,
            )
    client.batch = {
        "p-old-ok": eligible(old_ok, "p-old-ok"),
        "p-old-bad": pair(old_bad, "p-old-bad", liquidity="500"),
        "p-young": eligible(young, "p-young"),
    }
    stats = new_stats()
    await service.recheck(stats)

    assert stats["recheck_tokens"] == 2  # the token first seen today is not rechecked
    assert stats["scored"] == 2 and stats["eligible"] == 1
    assert stats["rejection_reasons"]["Liquidity is below USD 50,000"] == 1
    assert {r["token_address"] for r in await checks(db)} == {old_ok}
    assert {r["trigger_source"] for r in await checks(db)} == {"recheck"}
    assert stats["recheck_universe"] == 2
    async with db.acquire() as conn:
        stored = await conn.fetch(
            "SELECT token_address, score_inputs_json FROM research_candidates WHERE score_inputs_json ? 'discovery_source'"
        )
    assert [r["token_address"] for r in stored] == [old_ok]
    assert json.loads(stored[0]["score_inputs_json"])["discovery_source"] == "recheck"
