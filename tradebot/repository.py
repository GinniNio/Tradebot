from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from tradebot.serialization import json_text


class InvalidCandidateTransition(ValueError):
    pass


@dataclass(frozen=True)
class SourceEventInput:
    provider: str
    provider_event_id: str | None
    raw_payload_json: dict[str, Any]
    event_at: datetime | None
    fetched_at: datetime
    token_address: str | None
    pair_address: str | None
    chain: str


class Repository:
    def __init__(self, conn: Any):
        self.conn = conn

    async def upsert_source_event(self, item: SourceEventInput) -> Any:
        return await self.conn.fetchrow(
            """
            INSERT INTO source_events (
                provider, provider_event_id, raw_payload_json, event_at, fetched_at,
                token_address, pair_address, chain
            )
            VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, $8)
            ON CONFLICT (provider, provider_event_id)
            WHERE provider_event_id IS NOT NULL
            DO UPDATE SET fetched_at = EXCLUDED.fetched_at
            RETURNING *
            """,
            item.provider,
            item.provider_event_id,
            json_text(item.raw_payload_json),
            item.event_at,
            item.fetched_at,
            item.token_address,
            item.pair_address,
            item.chain,
        )

    async def create_quote_snapshot(
        self,
        idempotency_key: str,
        candidate_id: UUID,
        strategy_version_id: UUID,
        side: str,
        input_mint: str,
        output_mint: str,
        input_amount_base_units: Decimal,
        output_amount_base_units: Decimal,
        price_impact_pct: Decimal | None,
        route_json: dict[str, Any],
        status: str,
        failure_reason: str | None,
        git_commit_sha: str,
    ) -> Any:
        return await self.conn.fetchrow(
            """
            INSERT INTO quote_snapshots (
                idempotency_key, candidate_id, strategy_version_id, side,
                input_mint, output_mint, input_amount_base_units,
                output_amount_base_units, price_impact_pct, route_json, status,
                failure_reason, git_commit_sha
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12)
            ON CONFLICT (idempotency_key) DO UPDATE SET
                captured_at = quote_snapshots.captured_at
            RETURNING *
            """,
            idempotency_key,
            candidate_id,
            strategy_version_id,
            side,
            input_mint,
            output_mint,
            input_amount_base_units,
            output_amount_base_units,
            price_impact_pct,
            json_text(route_json),
            status,
            failure_reason,
            git_commit_sha,
        )

    async def list_candidates(self, limit: int = 3) -> list[Any]:
        rows = await self.conn.fetch(
            """
            SELECT rc.*, row_number() OVER (ORDER BY rc.score DESC NULLS LAST, rc.created_at DESC) AS rank,
                   ms.liquidity_usd, ms.volume_1h_usd, ms.volume_24h_usd,
                   ms.price_usd, ms.price_change_1h_pct, ms.price_change_24h_pct,
                   ms.market_cap_usd, ms.fdv_usd, cd.operator_action, cd.operator_note,
                   COALESCE(oc.total, 0) outcome_total, COALESCE(oc.completed, 0) outcome_completed
            FROM research_candidates rc
            LEFT JOIN LATERAL (
                SELECT * FROM market_snapshots WHERE candidate_id=rc.id ORDER BY captured_at DESC LIMIT 1
            ) ms ON true
            LEFT JOIN candidate_decisions cd ON cd.candidate_id=rc.id
            LEFT JOIN LATERAL (
                SELECT count(*) total, count(*) FILTER (WHERE status='completed') completed
                FROM outcome_checks WHERE candidate_id=rc.id
            ) oc ON true
            WHERE rc.state IN ('eligible','watched') AND rc.expires_at > now()
            ORDER BY CASE WHEN rc.state IN ('eligible','watched') THEN 0 ELSE 1 END, rc.score DESC NULLS LAST, rc.created_at DESC
            LIMIT $1
            """,
            limit,
        )
        return list(rows)

    async def list_candidate_outcomes(self, candidate_id: UUID) -> list[Any]:
        return list(
            await self.conn.fetch(
                """SELECT check_kind, due_at, status, completed_at, baseline_price_usd,
                outcome_price_usd, market_price_change_pct, outcome_method, error
                FROM outcome_checks WHERE candidate_id=$1
                ORDER BY CASE check_kind WHEN '1h' THEN 1 WHEN '24h' THEN 2 WHEN '3d' THEN 3 ELSE 4 END""",
                candidate_id,
            )
        )

    async def list_rejected_candidates(self, limit: int = 20) -> list[Any]:
        return list(
            await self.conn.fetch(
                """SELECT rc.*, ms.price_usd, cd.operator_action,
                COALESCE(oc.total, 0) outcome_total, COALESCE(oc.completed, 0) outcome_completed
                FROM research_candidates rc
                LEFT JOIN LATERAL (
                    SELECT price_usd FROM market_snapshots WHERE candidate_id=rc.id
                    ORDER BY captured_at DESC LIMIT 1
                ) ms ON true
                LEFT JOIN candidate_decisions cd ON cd.candidate_id=rc.id
                LEFT JOIN LATERAL (
                    SELECT count(*) total, count(*) FILTER (WHERE status='completed') completed
                    FROM outcome_checks WHERE candidate_id=rc.id
                ) oc ON true
                WHERE rc.state IN ('rejected','expired')
                ORDER BY rc.created_at DESC LIMIT $1""",
                limit,
            )
        )

    async def set_research_paused(self, paused: bool) -> bool:
        return bool(
            await self.conn.fetchval(
                "UPDATE operator_settings SET research_paused=$1, updated_at=now() WHERE id=true RETURNING research_paused",
                paused,
            )
        )

    async def get_research_paused(self) -> bool:
        return bool(
            await self.conn.fetchval(
                "SELECT research_paused FROM operator_settings WHERE id=true"
            )
        )

    async def decide_candidate(
        self, candidate_id: UUID, action: str, note: str | None, git_commit_sha: str
    ) -> Any:
        if action not in {"watch", "reject"}:
            raise ValueError("action must be watch or reject")
        candidate = await self.conn.fetchrow(
            """SELECT rc.id, rc.strategy_version_id, rc.state, rc.expires_at,
            (SELECT 1 + count(*) FROM research_candidates ranked
             WHERE ranked.state IN ('eligible','watched') AND ranked.score > rc.score) AS candidate_rank
            FROM research_candidates rc WHERE rc.id=$1 FOR UPDATE""",
            candidate_id,
        )
        if not candidate:
            return None
        if candidate["state"] not in {"eligible", "watched"}:
            raise InvalidCandidateTransition("candidate is not actionable")
        if candidate["expires_at"] is None or candidate["expires_at"] <= datetime.now(
            candidate["expires_at"].tzinfo
        ):
            raise InvalidCandidateTransition("candidate has expired")
        baseline = await self.conn.fetchrow(
            """SELECT id, price_usd FROM market_snapshots
            WHERE candidate_id=$1 AND price_usd > 0 ORDER BY captured_at DESC LIMIT 1""",
            candidate_id,
        )
        if not baseline:
            raise InvalidCandidateTransition(
                "candidate has no valid decision-time price"
            )
        key = f"decision:{candidate_id}"
        row = await self.conn.fetchrow(
            """INSERT INTO candidate_decisions
            (idempotency_key,candidate_id,strategy_version_id,git_commit_sha,rank,baseline_market_snapshot_id,baseline_price_usd,operator_action,operator_note,decided_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,now()) ON CONFLICT (candidate_id) DO UPDATE SET
            operator_action=EXCLUDED.operator_action, operator_note=EXCLUDED.operator_note,
            decided_at=now() RETURNING *""",
            key,
            candidate_id,
            candidate["strategy_version_id"],
            git_commit_sha,
            candidate["candidate_rank"],
            baseline["id"],
            baseline["price_usd"],
            action,
            note,
        )
        await self.conn.execute(
            "UPDATE research_candidates SET state=$2 WHERE id=$1",
            candidate_id,
            "watched" if action == "watch" else "rejected",
        )
        for kind, interval in (
            ("1h", "1 hour"),
            ("24h", "24 hours"),
            ("3d", "3 days"),
            ("7d", "7 days"),
        ):
            await self.conn.execute(
                """INSERT INTO outcome_checks (idempotency_key,candidate_id,check_kind,due_at,status,outcome_method,baseline_market_snapshot_id,baseline_price_usd)
                VALUES ($1,$2,$3,now()+$4::interval,'pending','dexscreener_market_price',$5,$6)
                ON CONFLICT (idempotency_key) DO NOTHING""",
                f"outcome:{candidate_id}:{kind}",
                candidate_id,
                kind,
                interval,
                baseline["id"],
                baseline["price_usd"],
            )
        return row
