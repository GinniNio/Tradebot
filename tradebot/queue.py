from __future__ import annotations

from typing import Any
from uuid import UUID

CLAIM_DUE_OUTCOME_CHECKS_SQL = """
WITH due AS (
    SELECT id
    FROM outcome_checks
    WHERE (status = 'pending' AND due_at <= now())
       OR (status = 'retryable' AND due_at <= now())
    ORDER BY due_at
    LIMIT $1
    FOR UPDATE SKIP LOCKED
)
UPDATE outcome_checks AS oc
SET status = 'claimed'
FROM due
WHERE oc.id = due.id
RETURNING oc.*
"""


class ResearchQueue:
    def __init__(self, conn: Any):
        self.conn = conn

    async def claim_due_outcome_checks(self, limit: int = 100) -> list[Any]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        rows = await self.conn.fetch(CLAIM_DUE_OUTCOME_CHECKS_SQL, limit)
        return list(rows)

    async def complete_outcome_check(
        self,
        check_id: UUID,
        value_input_base_units: str,
        value_output_base_units: str,
        quote_snapshot_id: UUID | None,
    ) -> None:
        await self.conn.execute(
            """
            UPDATE outcome_checks
            SET status = 'completed',
                completed_at = now(),
                value_input_base_units = $2,
                value_output_base_units = $3,
                quote_snapshot_id = $4,
                error = NULL
            WHERE id = $1
            """,
            check_id,
            value_input_base_units,
            value_output_base_units,
            quote_snapshot_id,
        )

    async def fail_outcome_check(self, check_id: UUID, error: str) -> None:
        await self.conn.execute(
            """
            UPDATE outcome_checks
            SET status = 'failed', completed_at = now(), error = $2
            WHERE id = $1
            """,
            check_id,
            error[:500],
        )

    async def retry_outcome_check(self, check_id: UUID, error_category: str) -> None:
        await self.conn.execute(
            "UPDATE outcome_checks SET status='retryable', error=$2, due_at=now() + interval '15 minutes' WHERE id=$1",
            check_id,
            error_category[:100],
        )

    async def complete_market_outcome(
        self, check_id: UUID, market_snapshot_id: UUID, price_usd: str
    ) -> None:
        await self.conn.execute(
            """UPDATE outcome_checks SET status='completed', completed_at=now(), market_snapshot_id=$2,
            outcome_price_usd=$3, outcome_method='dexscreener_market_price',
            market_price_change_pct=CASE WHEN baseline_price_usd > 0
              THEN (($3::numeric - baseline_price_usd) / baseline_price_usd) * 100 ELSE NULL END,
            error=NULL WHERE id=$1""",
            check_id,
            market_snapshot_id,
            price_usd,
        )
