from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID


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
            item.raw_payload_json,
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
            route_json,
            status,
            failure_reason,
            git_commit_sha,
        )
