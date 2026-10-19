"""record which discovery path started each measurement

Revision ID: 0004_trigger_source
Revises: 0003_research_experiment
"""

import sqlalchemy as sa

from alembic import op

revision = "0004_trigger_source"
down_revision = "0003_research_experiment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outcome_checks", sa.Column("trigger_source", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_outcome_checks_trigger_source",
        "outcome_checks",
        "trigger_source IS NULL OR trigger_source IN ('profile_feed','recheck')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_outcome_checks_trigger_source", "outcome_checks")
    op.drop_column("outcome_checks", "trigger_source")
