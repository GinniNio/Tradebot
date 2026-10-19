"""automatic research experiment

Revision ID: 0003_research_experiment
Revises: 0002_market_watch_queue
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003_research_experiment"
down_revision = "0002_market_watch_queue"
branch_labels = None
depends_on = None

OUTCOME_COLUMNS = (
    sa.Column("cohort", sa.Text(), nullable=True),
    sa.Column(
        "strategy_version_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("strategy_versions.id"),
        nullable=True,
    ),
    sa.Column("token_address", sa.Text(), nullable=True),
    sa.Column("scheduled_at", sa.TIMESTAMP(timezone=True), nullable=True),
    sa.Column("claimed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
    sa.Column("last_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
    sa.Column("resolution", sa.Text(), nullable=True),
    sa.Column("outcome_liquidity_usd", sa.Numeric(50, 30), nullable=True),
    sa.Column("sol_baseline_price_usd", sa.Numeric(50, 30), nullable=True),
    sa.Column("sol_outcome_price_usd", sa.Numeric(50, 30), nullable=True),
)


def upgrade() -> None:
    for column in OUTCOME_COLUMNS:
        op.add_column("outcome_checks", column)
    # The original schedule is never rewritten; retries move due_at only.
    op.execute("UPDATE outcome_checks SET scheduled_at = due_at WHERE scheduled_at IS NULL")
    op.create_check_constraint(
        "ck_outcome_checks_cohort",
        "outcome_checks",
        "cohort IS NULL OR cohort IN ('eligible','control')",
    )
    op.create_check_constraint(
        "ck_outcome_checks_status",
        "outcome_checks",
        "status IN ('pending','claimed','retryable','completed','failed','unresolved')",
    )
    op.create_index(
        "idx_outcome_checks_experiment",
        "outcome_checks",
        ["strategy_version_id", "cohort", "check_kind"],
    )

    op.create_table(
        "scan_cycles",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("strategy_version", sa.Text(), nullable=False),
        sa.Column("git_commit_sha", sa.Text(), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("stats_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint("status IN ('running','ok','error')", name="ck_scan_cycles_status"),
    )
    op.create_index("idx_scan_cycles_started", "scan_cycles", ["started_at"])
    op.create_index(
        "idx_research_candidates_token_created",
        "research_candidates",
        ["token_address", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_research_candidates_token_created", table_name="research_candidates")
    op.drop_index("idx_scan_cycles_started", table_name="scan_cycles")
    op.drop_table("scan_cycles")
    op.drop_index("idx_outcome_checks_experiment", table_name="outcome_checks")
    op.drop_constraint("ck_outcome_checks_status", "outcome_checks")
    op.drop_constraint("ck_outcome_checks_cohort", "outcome_checks")
    for column in reversed(OUTCOME_COLUMNS):
        op.drop_column("outcome_checks", column.name)
