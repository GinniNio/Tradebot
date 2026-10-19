from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from tradebot import experiment
from tradebot.dexscreener import (
    DexscreenerClient,
    ProviderError,
    normalize_pair,
    select_pair,
)
from tradebot.queue import ResearchQueue
from tradebot.serialization import json_text
from tradebot.strategy import FROZEN_CONFIG, STRATEGY_KEY, STRATEGY_VERSION, evaluate


def valid_price(value: Any) -> Decimal | None:
    try:
        price = Decimal(str(value))
        return price if price.is_finite() and price > 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def new_stats() -> dict[str, Any]:
    return {
        "profile_tokens": 0,
        "recheck_tokens": 0,
        "recheck_pairs_found": 0,
        "provider_errors": 0,
        "scored": 0,
        "passed_hard_filters": 0,
        "eligible": 0,
        "eligible_tokens_started": 0,
        "control_tokens_started": 0,
        "missing_baseline_price": 0,
        "sol_price_missing": 0,
        "outcomes_completed": 0,
        "outcomes_retried": 0,
        "outcomes_unresolved": 0,
        "recheck_universe": 0,
        # Rule failures where the input was present, and rejections caused by a
        # missing or unparseable input, kept apart.
        "rejection_reasons": {},
        "missing_inputs": {},
    }


class MarketWatchService:
    def __init__(self, db: Any, client: DexscreenerClient, git_commit_sha: str):
        self.db = db
        self.client = client
        self.git_commit_sha = git_commit_sha

    async def run_cycle(self) -> None:
        stats = new_stats()
        async with self.db.acquire() as conn:
            cycle_id = await conn.fetchval(
                """INSERT INTO scan_cycles(strategy_version,git_commit_sha,started_at,status)
                VALUES($1,$2,now(),'running') RETURNING id""",
                STRATEGY_VERSION,
                self.git_commit_sha,
            )
        try:
            await self.process_due_outcomes(stats)
            async with self.db.acquire() as conn:
                paused = bool(
                    await conn.fetchval(
                        "SELECT research_paused FROM operator_settings WHERE id=true"
                    )
                )
            if not paused:
                sol_price = await self._sol_price(stats)
                await self.discover(stats, sol_price)
                await self.recheck(stats, sol_price)
        except Exception as exc:
            await self._finish_cycle(cycle_id, "error", stats, type(exc).__name__)
            raise
        await self._finish_cycle(cycle_id, "ok", stats)

    async def _finish_cycle(
        self, cycle_id: Any, status: str, stats: dict[str, Any], error: str | None = None
    ) -> None:
        async with self.db.acquire() as conn:
            await conn.execute(
                """UPDATE scan_cycles SET finished_at=now(), status=$2, error=$3,
                stats_json=$4::jsonb WHERE id=$1""",
                cycle_id,
                status,
                error,
                json_text(stats),
            )

    async def _sol_price(self, stats: dict[str, Any]) -> Decimal | None:
        try:
            price = await self.client.sol_price_usd(experiment.WSOL_MINT)
        except ProviderError:
            price = None
        if price is None:
            stats["sol_price_missing"] += 1
        return price

    @staticmethod
    def _count(stats: dict[str, Any], result: dict[str, Any]) -> None:
        stats["scored"] += 1
        stats["passed_hard_filters"] += bool(result["passed_hard_filters"])
        stats["eligible"] += bool(result["eligible"])
        for detail in result["rejection_details"]:
            bucket = stats["missing_inputs" if detail["missing_input"] else "rejection_reasons"]
            bucket[detail["reason"]] = bucket.get(detail["reason"], 0) + 1

    async def _strategy_id(self, conn: Any) -> Any:
        return await conn.fetchval(
            """INSERT INTO strategy_versions(strategy_key,version,git_commit_sha,config_json)
            VALUES($1,$2,$3,$4::jsonb) ON CONFLICT(strategy_key,version) DO UPDATE SET version=EXCLUDED.version RETURNING id""",
            STRATEGY_KEY,
            STRATEGY_VERSION,
            self.git_commit_sha,
            json_text({**FROZEN_CONFIG.json(), "experiment": experiment.config_json()}),
        )

    async def discover(
        self, stats: dict[str, Any] | None = None, sol_price: Decimal | None = None
    ) -> None:
        stats = stats if stats is not None else new_stats()
        profiles = await self.client.discover()
        for profile in profiles[:30]:
            token = profile.get("tokenAddress")
            if not token:
                continue
            stats["profile_tokens"] += 1
            try:
                pair = select_pair(await self.client.pairs_for_token(token))
            except ProviderError:
                stats["provider_errors"] += 1
                continue
            if pair:
                await self._persist_pair(pair, sol_price, "profile_feed", stats)

    async def recheck(
        self, stats: dict[str, Any] | None = None, sol_price: Decimal | None = None
    ) -> None:
        """Revisit tokens first discovered 1-7 days ago.

        The profile feed only surfaces tokens hours old, which almost never meet
        the liquidity and volume floors. A recheck that does not pass the hard
        filters is counted but not stored, to keep storage bounded.
        """
        stats = stats if stats is not None else new_stats()
        rotation = str(int(time.time() // 900))
        async with self.db.acquire() as conn:
            rows = await conn.fetch(
                """SELECT token_address,
                    (array_agg(selected_pair ORDER BY created_at DESC))[1] AS pair_address,
                    count(*) OVER () AS universe
                FROM research_candidates WHERE selected_pair IS NOT NULL
                GROUP BY token_address
                HAVING min(created_at) BETWEEN now() - $1::interval AND now() - $2::interval
                ORDER BY md5(token_address || $3) LIMIT $4""",
                experiment.RECHECK_MAX_AGE,
                experiment.RECHECK_MIN_AGE,
                rotation,
                experiment.RECHECK_TOKENS_PER_CYCLE,
            )
        stats["recheck_tokens"] += len(rows)
        stats["recheck_universe"] = int(rows[0]["universe"]) if rows else 0
        try:
            pairs = await self.client.pairs_batch([r["pair_address"] for r in rows])
        except ProviderError:
            stats["provider_errors"] += 1
            return
        stats["recheck_pairs_found"] += len(pairs)
        for pair in pairs.values():
            result = evaluate(normalize_pair(pair))
            if result["passed_hard_filters"]:
                await self._persist_pair(pair, sol_price, "recheck", stats, result)
            else:
                self._count(stats, result)

    async def _persist_pair(
        self,
        pair: dict[str, Any],
        sol_price: Decimal | None = None,
        source: str = "profile_feed",
        stats: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        stats = stats if stats is not None else new_stats()
        snap = normalize_pair(pair)
        result = result or evaluate(snap)
        self._count(stats, result)
        score_inputs = {
            **result["score_inputs"],
            "discovery_source": source,
            "passed_hard_filters": result["passed_hard_filters"],
        }
        pair_address = snap["pair_address"]
        token = snap["token_address"]
        event_key = hashlib.sha256(
            f"dexscreener:{pair_address}:{snap['captured_at'].replace(minute=(snap['captured_at'].minute // 15) * 15, second=0, microsecond=0).isoformat()}".encode()
        ).hexdigest()
        async with self.db.acquire() as conn, conn.transaction():
            strategy_id = await self._strategy_id(conn)
            source_id = await conn.fetchval(
                """INSERT INTO source_events(provider,provider_event_id,raw_payload_json,fetched_at,token_address,pair_address,chain)
                    VALUES('dexscreener',$1,$2::jsonb,$3,$4,$5,'solana') ON CONFLICT(provider,provider_event_id)
                    WHERE provider_event_id IS NOT NULL DO UPDATE SET fetched_at=EXCLUDED.fetched_at RETURNING id""",
                event_key,
                json_text(pair),
                snap["captured_at"],
                token,
                pair_address,
            )
            recent = await conn.fetchrow(
                """SELECT rc.id, rc.state, cd.id AS decision_id FROM research_candidates rc
                LEFT JOIN candidate_decisions cd ON cd.candidate_id=rc.id
                WHERE rc.token_address=$1 AND rc.selected_pair=$2 AND rc.strategy_version_id=$3
                AND rc.created_at > now()-interval '24 hours' ORDER BY rc.created_at DESC LIMIT 1 FOR UPDATE OF rc""",
                token,
                pair_address,
                strategy_id,
            )
            if recent:
                snapshot_id = await self._insert_snapshot(
                    conn, recent["id"], source_id, snap, pair
                )
                if (
                    result["eligible"]
                    and recent["state"] == "rejected"
                    and not recent["decision_id"]
                ):
                    await conn.execute(
                        """UPDATE research_candidates SET state='eligible', score=$2,
                        score_inputs_json=$3::jsonb, score_components_json=$4::jsonb,
                        hard_rejects_json='[]'::jsonb, ranking_reasons_json=$5::jsonb,
                        expires_at=now()+interval '24 hours' WHERE id=$1""",
                        recent["id"],
                        result["score"],
                        json_text(score_inputs),
                        json_text(result["score_components"]),
                        json_text(result["explanations"]),
                    )
                await self._start_experiment(
                    conn, recent["id"], snapshot_id, strategy_id, snap, result, sol_price, stats, source
                )
                return result
            candidate_key = hashlib.sha256(
                f"{token}:{pair_address}:{strategy_id}:{snap['captured_at'].isoformat()}".encode()
            ).hexdigest()
            candidate_id = await conn.fetchval(
                """INSERT INTO research_candidates(idempotency_key,token_address,token_symbol,chain,selected_pair,strategy_version_id,state,score,score_inputs_json,score_components_json,hard_rejects_json,ranking_reasons_json,expires_at)
                    VALUES($1,$2,$3,'solana',$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10::jsonb,$11::jsonb,now()+interval '24 hours') RETURNING id""",
                candidate_key,
                token,
                snap["token_symbol"],
                pair_address,
                strategy_id,
                "eligible" if result["eligible"] else "rejected",
                result["score"],
                json_text(score_inputs),
                json_text(result["score_components"]),
                json_text(result["rejection_reasons"]),
                json_text(result["explanations"]),
            )
            snapshot_id = await self._insert_snapshot(
                conn, candidate_id, source_id, snap, pair
            )
            await self._start_experiment(
                conn, candidate_id, snapshot_id, strategy_id, snap, result, sol_price, stats, source
            )
        return result

    async def _start_experiment(
        self,
        conn: Any,
        candidate_id: Any,
        snapshot_id: Any,
        strategy_id: Any,
        snap: dict[str, Any],
        result: dict[str, Any],
        sol_price: Decimal | None,
        stats: dict[str, Any],
        source: str = "profile_feed",
    ) -> None:
        """Measure from the first qualifying observation of each token, once.

        Called on every observation, not only on new rows, so a token that
        scored 60 on Monday and 70 on Thursday starts at Thursday's price. The
        idempotency key makes later observations of the same token no-ops, and
        an earlier control record is never rewritten.
        """
        if result["eligible"]:
            cohort = "eligible"
        elif result["passed_hard_filters"] and experiment.in_control_sample(
            snap["token_address"]
        ):
            already_eligible = await conn.fetchval(
                """SELECT 1 FROM outcome_checks WHERE strategy_version_id=$1
                AND token_address=$2 AND cohort='eligible' LIMIT 1""",
                strategy_id,
                snap["token_address"],
            )
            if already_eligible:
                return
            cohort = "control"
        else:
            return
        price = valid_price(snap["price_usd"])
        if price is None:
            stats["missing_baseline_price"] += 1
            return
        inserted = 0
        for kind, (after, _tolerance) in experiment.HORIZONS.items():
            status = await conn.execute(
                """INSERT INTO outcome_checks (idempotency_key,candidate_id,check_kind,due_at,
                scheduled_at,status,outcome_method,baseline_market_snapshot_id,baseline_price_usd,
                cohort,strategy_version_id,token_address,sol_baseline_price_usd,trigger_source)
                VALUES ($1,$2,$3,$4,$4,'pending','dexscreener_market_price',$5,$6,$7,$8,$9,$10,$11)
                ON CONFLICT (idempotency_key) DO NOTHING""",
                f"exp:{strategy_id}:{cohort}:{snap['token_address']}:{kind}",
                candidate_id,
                kind,
                snap["captured_at"] + after,
                snapshot_id,
                price,
                cohort,
                strategy_id,
                snap["token_address"],
                sol_price,
                source,
            )
            inserted += str(status).endswith(" 1")
        if inserted:
            stats[f"{cohort}_tokens_started"] += 1

    async def _insert_snapshot(
        self,
        conn: Any,
        candidate_id: Any,
        source_id: Any,
        snap: dict[str, Any],
        pair: dict[str, Any],
    ) -> Any:
        return await conn.fetchval(
            """INSERT INTO market_snapshots(candidate_id,source_event_id,pair_address,liquidity_usd,volume_1h_usd,volume_6h_usd,volume_24h_usd,price_usd,price_change_1h_pct,price_change_6h_pct,price_change_24h_pct,market_cap_usd,fdv_usd,captured_at,raw_payload_json)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15::jsonb) RETURNING id""",
            candidate_id,
            source_id,
            snap["pair_address"],
            snap["liquidity_usd"],
            snap["volume_1h_usd"],
            snap["volume_6h_usd"],
            snap["volume_24h_usd"],
            snap["price_usd"],
            snap["price_change_1h_pct"],
            snap["price_change_6h_pct"],
            snap["price_change_24h_pct"],
            snap["market_cap_usd"],
            snap["fdv_usd"],
            snap["captured_at"],
            json_text(pair),
        )

    async def process_due_outcomes(self, stats: dict[str, Any] | None = None) -> None:
        stats = stats if stats is not None else new_stats()
        async with self.db.acquire() as conn, conn.transaction():
            checks = await ResearchQueue(conn).claim_due_outcome_checks(60)
        if not checks:
            return
        now = datetime.now(UTC)
        live = []
        for check in checks:
            if now > self._window_closes(check):
                # Too late to be a price for this horizon. Record the gap.
                await self._close(check, "missed_window", stats)
            else:
                live.append(check)
        if not live:
            return
        async with self.db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, selected_pair FROM research_candidates WHERE id = ANY($1::uuid[])",
                list({check["candidate_id"] for check in live}),
            )
        pair_of = {row["id"]: row["selected_pair"] for row in rows}
        fetch_error = None
        try:
            pairs = await self.client.pairs_batch(list(pair_of.values()))
        except ProviderError:
            pairs, fetch_error = {}, "provider_error"
        sol_price = await self._sol_price(stats)
        for check in live:
            try:
                pair = pairs.get(pair_of.get(check["candidate_id"]))
                if fetch_error:
                    await self._retry_or_close(check, fetch_error, stats)
                    continue
                if pair is None:
                    await self._retry_or_close(check, "pair_missing", stats)
                    continue
                snap = normalize_pair(pair)
                price = valid_price(snap["price_usd"])
                if price is None:
                    await self._retry_or_close(check, "invalid_price", stats)
                    continue
                async with self.db.acquire() as conn, conn.transaction():
                    snapshot_id = await self._insert_snapshot(
                        conn, check["candidate_id"], None, snap, pair
                    )
                    await ResearchQueue(conn).complete_market_outcome(
                        check["id"],
                        snapshot_id,
                        str(price),
                        None if snap["liquidity_usd"] is None else str(snap["liquidity_usd"]),
                        None if sol_price is None else str(sol_price),
                    )
                stats["outcomes_completed"] += 1
            except Exception as exc:  # noqa: BLE001 - every claimed row must become retryable
                await self._retry_or_close(check, type(exc).__name__, stats)

    @staticmethod
    def _window_closes(check: Any) -> datetime:
        scheduled = check["scheduled_at"] or check["due_at"]
        _after, tolerance = experiment.HORIZONS.get(
            check["check_kind"], (None, experiment.HORIZONS["24h"][1])
        )
        return scheduled + tolerance

    async def _retry_or_close(self, check: Any, reason: str, stats: dict[str, Any]) -> None:
        next_attempt = datetime.now(UTC) + experiment.RETRY_DELAY
        if next_attempt > self._window_closes(check):
            await self._close(check, reason, stats)
            return
        async with self.db.acquire() as conn, conn.transaction():
            await ResearchQueue(conn).retry_outcome_check(check["id"], reason)
        stats["outcomes_retried"] += 1

    async def _close(self, check: Any, reason: str, stats: dict[str, Any]) -> None:
        async with self.db.acquire() as conn, conn.transaction():
            await ResearchQueue(conn).mark_unresolved(check["id"], reason)
        stats["outcomes_unresolved"] += 1
