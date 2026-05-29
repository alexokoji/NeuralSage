"""strategy_observations table for cross-agent learning

Revision ID: 0002_strategy_observations
Revises: 0001_initial
Create Date: 2026-05-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_strategy_observations"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategy_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("strategy_type", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column(
            "source_agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "source_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("params", postgresql.JSONB, nullable=False),
        sa.Column("backtest_score", sa.Numeric(10, 6), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(20, 8), server_default="0"),
        sa.Column("realized_trades", sa.Integer, server_default="0"),
        sa.Column("candle_window_start", sa.DateTime(timezone=True)),
        sa.Column("candle_window_end", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_strategy_observations_strategy_type",
        "strategy_observations",
        ["strategy_type"],
    )
    op.create_index(
        "ix_strategy_observations_symbol",
        "strategy_observations",
        ["symbol"],
    )
    op.create_index(
        "ix_obs_lookup",
        "strategy_observations",
        ["strategy_type", "symbol", "timeframe", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_obs_lookup", table_name="strategy_observations")
    op.drop_index("ix_strategy_observations_symbol", table_name="strategy_observations")
    op.drop_index("ix_strategy_observations_strategy_type", table_name="strategy_observations")
    op.drop_table("strategy_observations")
