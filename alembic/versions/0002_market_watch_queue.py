"""daily market watch queue

Revision ID: 0002_market_watch_queue
Revises: 0001_managed_foundation
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002_market_watch_queue"
down_revision = "0001_managed_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "research_candidates", sa.Column("idempotency_key", sa.Text(), nullable=True)
    )
    op.add_column(
        "research_candidates", sa.Column("token_symbol", sa.Text(), nullable=True)
    )
    op.add_column(
        "research_candidates", sa.Column("score", sa.Numeric(8, 2), nullable=True)
    )
    op.add_column(
        "research_candidates",
        sa.Column(
            "score_inputs_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "research_candidates",
        sa.Column(
            "score_components_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "research_candidates",
        sa.Column(
            "hard_rejects_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "research_candidates",
        sa.Column(
            "ranking_reasons_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_unique_constraint(
        "uq_research_candidates_idempotency_key",
        "research_candidates",
        ["idempotency_key"],
    )
    op.create_check_constraint(
        "ck_research_candidates_state",
        "research_candidates",
        "state IN ('screened','eligible','rejected','watched','expired')",
    )

    op.add_column(
        "market_snapshots",
        sa.Column(
            "source_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("source_events.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "market_snapshots", sa.Column("pair_address", sa.Text(), nullable=True)
    )
    op.add_column(
        "market_snapshots",
        sa.Column("market_cap_usd", sa.Numeric(50, 30), nullable=True),
    )
    op.add_column(
        "market_snapshots", sa.Column("fdv_usd", sa.Numeric(50, 30), nullable=True)
    )
    op.add_column(
        "market_snapshots",
        sa.Column("volume_6h_usd", sa.Numeric(50, 30), nullable=True),
    )
    op.add_column(
        "market_snapshots",
        sa.Column("price_change_6h_pct", sa.Numeric(50, 30), nullable=True),
    )

    op.add_column(
        "candidate_decisions", sa.Column("idempotency_key", sa.Text(), nullable=True)
    )
    op.add_column(
        "candidate_decisions",
        sa.Column(
            "baseline_market_snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("market_snapshots.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "candidate_decisions",
        sa.Column("baseline_price_usd", sa.Numeric(50, 30), nullable=True),
    )
    op.create_unique_constraint(
        "uq_candidate_decisions_candidate", "candidate_decisions", ["candidate_id"]
    )
    op.create_unique_constraint(
        "uq_candidate_decisions_idempotency_key",
        "candidate_decisions",
        ["idempotency_key"],
    )
    op.create_check_constraint(
        "ck_candidate_decisions_action",
        "candidate_decisions",
        "operator_action IS NULL OR operator_action IN ('watch','reject')",
    )

    op.add_column(
        "outcome_checks", sa.Column("idempotency_key", sa.Text(), nullable=True)
    )
    op.add_column(
        "outcome_checks",
        sa.Column(
            "market_snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("market_snapshots.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "outcome_checks",
        sa.Column(
            "baseline_market_snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("market_snapshots.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "outcome_checks",
        sa.Column("baseline_price_usd", sa.Numeric(50, 30), nullable=True),
    )
    op.add_column(
        "outcome_checks",
        sa.Column("market_price_change_pct", sa.Numeric(50, 30), nullable=True),
    )
    op.add_column(
        "outcome_checks",
        sa.Column("outcome_price_usd", sa.Numeric(50, 30), nullable=True),
    )
    op.add_column(
        "outcome_checks",
        sa.Column(
            "outcome_method",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'dexscreener_market_price'"),
        ),
    )
    op.create_unique_constraint(
        "uq_outcome_checks_idempotency_key", "outcome_checks", ["idempotency_key"]
    )
    op.create_check_constraint(
        "ck_outcome_checks_method",
        "outcome_checks",
        "outcome_method = 'dexscreener_market_price'",
    )

    op.execute(
        "UPDATE research_candidates SET idempotency_key = id::text WHERE idempotency_key IS NULL"
    )
    op.execute(
        "UPDATE candidate_decisions SET idempotency_key = id::text WHERE idempotency_key IS NULL"
    )
    op.execute(
        "UPDATE outcome_checks SET idempotency_key = id::text WHERE idempotency_key IS NULL"
    )
    op.alter_column("research_candidates", "idempotency_key", nullable=False)
    op.alter_column("candidate_decisions", "idempotency_key", nullable=False)
    op.alter_column("outcome_checks", "idempotency_key", nullable=False)


def downgrade() -> None:
    for name in ("ck_outcome_checks_method", "uq_outcome_checks_idempotency_key"):
        op.drop_constraint(name, "outcome_checks")
    for column in (
        "market_price_change_pct",
        "baseline_price_usd",
        "baseline_market_snapshot_id",
        "outcome_method",
        "outcome_price_usd",
        "market_snapshot_id",
        "idempotency_key",
    ):
        op.drop_column("outcome_checks", column)
    for name in (
        "ck_candidate_decisions_action",
        "uq_candidate_decisions_idempotency_key",
        "uq_candidate_decisions_candidate",
    ):
        op.drop_constraint(name, "candidate_decisions")
    for column in (
        "baseline_price_usd",
        "baseline_market_snapshot_id",
        "idempotency_key",
    ):
        op.drop_column("candidate_decisions", column)
    for column in (
        "price_change_6h_pct",
        "volume_6h_usd",
        "fdv_usd",
        "market_cap_usd",
        "pair_address",
        "source_event_id",
    ):
        op.drop_column("market_snapshots", column)
    op.drop_constraint("ck_research_candidates_state", "research_candidates")
    op.drop_constraint("uq_research_candidates_idempotency_key", "research_candidates")
    for column in (
        "ranking_reasons_json",
        "hard_rejects_json",
        "score_components_json",
        "score_inputs_json",
        "score",
        "token_symbol",
        "idempotency_key",
    ):
        op.drop_column("research_candidates", column)
