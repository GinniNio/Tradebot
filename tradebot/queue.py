from __future__ import annotations

from typing import Any
from uuid import UUID

CLAIM_DUE_OUTCOME_CHECKS_SQL = """
WITH due AS (
    SELECT id
    FROM outcome_checks
    WHERE status = 'pending' AND due_at <= now()
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
