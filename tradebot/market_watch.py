from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation
from typing import Any

from tradebot.dexscreener import DexscreenerClient, normalize_pair, select_pair
from tradebot.queue import ResearchQueue
from tradebot.serialization import json_text
from tradebot.strategy import FROZEN_CONFIG, STRATEGY_KEY, STRATEGY_VERSION, evaluate


def valid_price(value: Any) -> Decimal | None:
    try:
        price = Decimal(str(value))
        return price if price.is_finite() and price > 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


class MarketWatchService:
    def __init__(self, db: Any, client: DexscreenerClient, git_commit_sha: str):
        self.db = db
        self.client = client
        self.git_commit_sha = git_commit_sha

    async def run_cycle(self) -> None:
        await self.process_due_outcomes()
        async with self.db.acquire() as conn:
            paused = bool(
                await conn.fetchval(
                    "SELECT research_paused FROM operator_settings WHERE id=true"
                )
            )
        if not paused:
            await self.discover()

    async def _strategy_id(self, conn: Any) -> Any:
        return await conn.fetchval(
            """INSERT INTO strategy_versions(strategy_key,version,git_commit_sha,config_json)
            VALUES($1,$2,$3,$4::jsonb) ON CONFLICT(strategy_key,version) DO UPDATE SET version=EXCLUDED.version RETURNING id""",
            STRATEGY_KEY,
            STRATEGY_VERSION,
            self.git_commit_sha,
            json_text(FROZEN_CONFIG.json()),
        )

    async def discover(self) -> None:
        profiles = await self.client.discover()
        for profile in profiles[:30]:
            token = profile.get("tokenAddress")
            if not token:
                continue
            pair = select_pair(await self.client.pairs_for_token(token))
            if pair:
                await self._persist_pair(pair)

    async def _persist_pair(self, pair: dict[str, Any]) -> None:
        snap = normalize_pair(pair)
        result = evaluate(snap)
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
                await self._insert_snapshot(conn, recent["id"], source_id, snap, pair)
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
                        json_text(result["score_inputs"]),
                        json_text(result["score_components"]),
                        json_text(result["explanations"]),
                    )
                return
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
                json_text(result["score_inputs"]),
                json_text(result["score_components"]),
                json_text(result["rejection_reasons"]),
                json_text(result["explanations"]),
            )
            await self._insert_snapshot(conn, candidate_id, source_id, snap, pair)

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

    async def process_due_outcomes(self) -> None:
        async with self.db.acquire() as conn, conn.transaction():
            checks = await ResearchQueue(conn).claim_due_outcome_checks(20)
        for check in checks:
            try:
                async with self.db.acquire() as conn:
                    pair_address = await conn.fetchval(
                        "SELECT selected_pair FROM research_candidates WHERE id=$1",
                        check["candidate_id"],
                    )
                pair = await self.client.pair(pair_address)
                snap = normalize_pair(pair)
                price = valid_price(snap["price_usd"])
                if price is None:
                    raise ValueError("invalid_price")
                async with self.db.acquire() as conn, conn.transaction():
                    snapshot_id = await conn.fetchval(
                        """INSERT INTO market_snapshots(candidate_id,pair_address,liquidity_usd,volume_1h_usd,volume_6h_usd,volume_24h_usd,price_usd,price_change_1h_pct,price_change_6h_pct,price_change_24h_pct,market_cap_usd,fdv_usd,captured_at,raw_payload_json)
                            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb) RETURNING id""",
                        check["candidate_id"],
                        pair_address,
                        snap["liquidity_usd"],
                        snap["volume_1h_usd"],
                        snap["volume_6h_usd"],
                        snap["volume_24h_usd"],
                        price,
                        snap["price_change_1h_pct"],
                        snap["price_change_6h_pct"],
                        snap["price_change_24h_pct"],
                        snap["market_cap_usd"],
                        snap["fdv_usd"],
                        snap["captured_at"],
                        json_text(pair),
                    )
                    await ResearchQueue(conn).complete_market_outcome(
                        check["id"], snapshot_id, str(price)
                    )
            except Exception as exc:  # noqa: BLE001 - every claimed row must become retryable
                async with self.db.acquire() as conn, conn.transaction():
                    await ResearchQueue(conn).retry_outcome_check(
                        check["id"], type(exc).__name__
                    )
