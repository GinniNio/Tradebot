"""managed foundation schema

Revision ID: 0001_managed_foundation
Revises:
Create Date: 2026-09-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0001_managed_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')

    op.create_table(
        "strategy_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("strategy_key", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("git_commit_sha", sa.Text(), nullable=False),
        sa.Column("config_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("strategy_key", "version", name="uq_strategy_versions_key_version"),
    )

    op.create_table(
        "source_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_event_id", sa.Text(), nullable=True),
        sa.Column("raw_payload_json", postgresql.JSONB(), nullable=False),
        sa.Column("event_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("token_address", sa.Text(), nullable=True),
        sa.Column("pair_address", sa.Text(), nullable=True),
        sa.Column("chain", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "uq_source_events_provider_event_id",
        "source_events",
        ["provider", "provider_event_id"],
        unique=True,
        postgresql_where=sa.text("provider_event_id IS NOT NULL"),
    )
    op.create_index("idx_source_events_token_chain", "source_events", ["token_address", "chain"])

    op.create_table(
        "research_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("token_address", sa.Text(), nullable=False),
        sa.Column("chain", sa.Text(), nullable=False),
        sa.Column("selected_pair", sa.Text(), nullable=True),
        sa.Column("strategy_version_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("strategy_versions.id"), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("idx_research_candidates_state", "research_candidates", ["state"])

    op.create_table(
        "market_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("research_candidates.id"), nullable=False),
        sa.Column("liquidity_usd", sa.Numeric(50, 30), nullable=True),
        sa.Column("volume_1h_usd", sa.Numeric(50, 30), nullable=True),
        sa.Column("volume_24h_usd", sa.Numeric(50, 30), nullable=True),
        sa.Column("price_usd", sa.Numeric(50, 30), nullable=True),
        sa.Column("price_change_1h_pct", sa.Numeric(50, 30), nullable=True),
        sa.Column("price_change_24h_pct", sa.Numeric(50, 30), nullable=True),
        sa.Column("tx_count_1h", sa.Integer(), nullable=True),
        sa.Column("captured_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("raw_payload_json", postgresql.JSONB(), nullable=False),
    )

    op.create_table(
        "quote_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("research_candidates.id"), nullable=False),
        sa.Column("strategy_version_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("strategy_versions.id"), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("input_mint", sa.Text(), nullable=False),
        sa.Column("output_mint", sa.Text(), nullable=False),
        sa.Column("input_amount_base_units", sa.Numeric(38, 0), nullable=False),
        sa.Column("output_amount_base_units", sa.Numeric(38, 0), nullable=False),
        sa.Column("price_impact_pct", sa.Numeric(50, 30), nullable=True),
        sa.Column("route_json", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("git_commit_sha", sa.Text(), nullable=False),
        sa.Column("captured_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("input_amount_base_units > 0", name="ck_quote_input_positive"),
        sa.CheckConstraint("output_amount_base_units >= 0", name="ck_quote_output_non_negative"),
        sa.UniqueConstraint("idempotency_key", name="uq_quote_snapshots_idempotency_key"),
    )

    op.create_table(
        "evidence_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("research_candidates.id"), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_transaction", sa.Text(), nullable=True),
        sa.Column("publisher", sa.Text(), nullable=True),
        sa.Column("source_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("classification", sa.Text(), nullable=False),
        sa.Column("raw_payload_json", postgresql.JSONB(), nullable=False),
    )

    op.create_table(
        "candidate_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("research_candidates.id"), nullable=False),
        sa.Column("strategy_version_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("strategy_versions.id"), nullable=False),
        sa.Column("git_commit_sha", sa.Text(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("hard_rejects_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("reasons_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("operator_action", sa.Text(), nullable=True),
        sa.Column("operator_note", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "outcome_checks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("research_candidates.id"), nullable=False),
        sa.Column("check_kind", sa.Text(), nullable=False),
        sa.Column("due_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("value_input_base_units", sa.Numeric(38, 0), nullable=True),
        sa.Column("value_output_base_units", sa.Numeric(38, 0), nullable=True),
        sa.Column("quote_snapshot_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("quote_snapshots.id"), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("idx_outcome_checks_due", "outcome_checks", ["status", "due_at"])

    op.create_table(
        "operator_settings",
        sa.Column("id", sa.Boolean(), primary_key=True, server_default=sa.text("true")),
        sa.Column("research_paused", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("id", name="ck_operator_settings_singleton"),
    )
    op.execute("INSERT INTO operator_settings (id, research_paused) VALUES (true, false) ON CONFLICT DO NOTHING")


def downgrade() -> None:
    op.drop_table("operator_settings")
    op.drop_index("idx_outcome_checks_due", table_name="outcome_checks")
    op.drop_table("outcome_checks")
    op.drop_table("candidate_decisions")
    op.drop_table("evidence_items")
    op.drop_table("quote_snapshots")
    op.drop_table("market_snapshots")
    op.drop_index("idx_research_candidates_state", table_name="research_candidates")
    op.drop_table("research_candidates")
    op.drop_index("idx_source_events_token_chain", table_name="source_events")
    op.drop_index("uq_source_events_provider_event_id", table_name="source_events")
    op.drop_table("source_events")
    op.drop_table("strategy_versions")
