"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(120), server_default=""),
        sa.Column("username", sa.String(64), unique=True),
        sa.Column("avatar_url", sa.String(512), server_default=""),
        sa.Column("timezone", sa.String(64), server_default="UTC"),
        sa.Column("risk_level", sa.String(8), server_default="medium"),
        sa.Column("daily_loss_limit", sa.Numeric(10, 4), server_default="5.0"),
        sa.Column("max_concurrent_trades", sa.Integer, server_default="5"),
        sa.Column("notifications_enabled", sa.Boolean, server_default=sa.true()),
        sa.Column("two_factor_enabled", sa.Boolean, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("risk_level IN ('low','medium','high')", name="ck_users_risk_level"),
    )
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("exchange", sa.String(32), nullable=False),
        sa.Column("label", sa.String(120), server_default=""),
        sa.Column("encrypted_api_key", sa.Text, nullable=False),
        sa.Column("encrypted_api_secret", sa.Text, nullable=False),
        sa.Column("encryption_iv", sa.String(64), nullable=False),
        sa.Column("permissions", postgresql.ARRAY(sa.String), server_default=sa.text("ARRAY['read','trade']::varchar[]")),
        sa.Column("is_active", sa.Boolean, server_default=sa.true()),
        sa.Column("is_testnet", sa.Boolean, server_default=sa.false()),
        sa.Column("verified", sa.Boolean, server_default=sa.false()),
        sa.Column("last_verified_at", sa.DateTime(timezone=True)),
        sa.Column("balance_cache", postgresql.JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column("balance_updated_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("exchange IN ('bybit','bitget','bybit_testnet')", name="ck_api_keys_exchange"),
        sa.CheckConstraint("NOT ('withdraw' = ANY(permissions))", name="ck_api_keys_no_withdraw"),
    )
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])

    op.create_table(
        "strategies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("description", sa.Text, server_default=""),
        sa.Column("default_params", postgresql.JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column("is_system", sa.Boolean, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "type IN ('ema_crossover','rsi_entry','breakout','micro_scalping')",
            name="ck_strategy_type",
        ),
    )

    op.create_table(
        "agents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("api_keys.id", ondelete="SET NULL")),
        sa.Column("strategy_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("strategies.id", ondelete="SET NULL")),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text, server_default=""),
        sa.Column("status", sa.String(16), server_default="idle"),
        sa.Column("assigned_capital", sa.Numeric(20, 8), server_default="0"),
        sa.Column("currency", sa.String(16), server_default="USDT"),
        sa.Column("trading_pairs", postgresql.ARRAY(sa.String), server_default=sa.text("ARRAY['BTCUSDT']::varchar[]")),
        sa.Column("timeframe", sa.String(8), server_default="15m"),
        sa.Column("max_risk_per_trade", sa.Numeric(6, 3), server_default="2.0"),
        sa.Column("daily_profit_target", sa.Numeric(8, 3), server_default="3.0"),
        sa.Column("weekly_profit_target", sa.Numeric(8, 3), server_default="10.0"),
        sa.Column("max_daily_loss", sa.Numeric(8, 3), server_default="5.0"),
        sa.Column("max_concurrent_trades", sa.Integer, server_default="3"),
        sa.Column("max_consecutive_losses", sa.Integer, server_default="3"),
        sa.Column("strategy_params", postgresql.JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column("ai_optimization_enabled", sa.Boolean, server_default=sa.true()),
        sa.Column("optimization_params", postgresql.JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column("total_pnl", sa.Numeric(20, 8), server_default="0"),
        sa.Column("total_trades", sa.Integer, server_default="0"),
        sa.Column("winning_trades", sa.Integer, server_default="0"),
        sa.Column("current_day_pnl", sa.Numeric(20, 8), server_default="0"),
        sa.Column("current_week_pnl", sa.Numeric(20, 8), server_default="0"),
        sa.Column("confidence_score", sa.Numeric(6, 3), server_default="50"),
        sa.Column("last_trade_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('idle','active','paused','stopped','error')", name="ck_agent_status"),
        sa.CheckConstraint("assigned_capital >= 0", name="ck_agent_capital_nonneg"),
        sa.CheckConstraint("max_risk_per_trade <= 5.0", name="ck_agent_risk_cap"),
        sa.CheckConstraint("timeframe IN ('1m','3m','5m','15m','30m','1h','4h','1d')", name="ck_agent_timeframe"),
        sa.CheckConstraint("confidence_score BETWEEN 0 AND 100", name="ck_agent_confidence"),
    )
    op.create_index("ix_agents_user_id", "agents", ["user_id"])
    op.create_index("ix_agents_status", "agents", ["status"])

    op.create_table(
        "trades",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="SET NULL")),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("api_keys.id", ondelete="SET NULL")),
        sa.Column("exchange", sa.String(32), nullable=False),
        sa.Column("exchange_order_id", sa.String(128)),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("order_type", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending"),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("entry_price", sa.Numeric(20, 8)),
        sa.Column("exit_price", sa.Numeric(20, 8)),
        sa.Column("stop_loss", sa.Numeric(20, 8)),
        sa.Column("take_profit", sa.Numeric(20, 8)),
        sa.Column("pnl", sa.Numeric(20, 8), server_default="0"),
        sa.Column("pnl_pct", sa.Numeric(10, 4), server_default="0"),
        sa.Column("fees", sa.Numeric(20, 8), server_default="0"),
        sa.Column("signal_source", sa.String(64), server_default="strategy"),
        sa.Column("signal_data", postgresql.JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column("risk_checks", postgresql.JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column("notes", sa.Text, server_default=""),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("side IN ('buy','sell')", name="ck_trade_side"),
        sa.CheckConstraint("order_type IN ('market','limit','stop_loss','take_profit')", name="ck_trade_order_type"),
        sa.CheckConstraint("status IN ('pending','open','filled','cancelled','rejected','expired')", name="ck_trade_status"),
        sa.CheckConstraint("quantity > 0", name="ck_trade_qty_positive"),
    )
    op.create_index("ix_trades_user_id", "trades", ["user_id"])
    op.create_index("ix_trades_agent_id", "trades", ["agent_id"])
    op.create_index("idx_trades_created", "trades", ["created_at"])

    op.create_table(
        "positions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="SET NULL")),
        sa.Column("trade_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("trades.id", ondelete="CASCADE")),
        sa.Column("exchange", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("entry_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("current_price", sa.Numeric(20, 8)),
        sa.Column("stop_loss", sa.Numeric(20, 8)),
        sa.Column("take_profit", sa.Numeric(20, 8)),
        sa.Column("unrealized_pnl", sa.Numeric(20, 8), server_default="0"),
        sa.Column("unrealized_pnl_pct", sa.Numeric(10, 4), server_default="0"),
        sa.Column("leverage", sa.Numeric(8, 2), server_default="1"),
        sa.Column("margin_used", sa.Numeric(20, 8), server_default="0"),
        sa.Column("is_open", sa.Boolean, server_default=sa.true()),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("side IN ('long','short')", name="ck_position_side"),
    )
    op.create_index("ix_positions_user_id", "positions", ["user_id"])
    op.create_index("idx_positions_open", "positions", ["is_open"])

    op.create_table(
        "agent_performance",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("snapshot_date", sa.Date, server_default=sa.text("CURRENT_DATE"), nullable=False),
        sa.Column("starting_capital", sa.Numeric(20, 8), server_default="0"),
        sa.Column("ending_capital", sa.Numeric(20, 8), server_default="0"),
        sa.Column("daily_pnl", sa.Numeric(20, 8), server_default="0"),
        sa.Column("daily_pnl_pct", sa.Numeric(10, 4), server_default="0"),
        sa.Column("total_trades", sa.Integer, server_default="0"),
        sa.Column("winning_trades", sa.Integer, server_default="0"),
        sa.Column("losing_trades", sa.Integer, server_default="0"),
        sa.Column("max_drawdown", sa.Numeric(10, 4), server_default="0"),
        sa.Column("sharpe_ratio", sa.Numeric(10, 4)),
        sa.Column("win_rate", sa.Numeric(6, 3), server_default="0"),
        sa.Column("avg_win", sa.Numeric(20, 8), server_default="0"),
        sa.Column("avg_loss", sa.Numeric(20, 8), server_default="0"),
        sa.Column("strategy_params_snapshot", postgresql.JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("agent_id", "snapshot_date", name="uq_agent_performance_day"),
    )

    op.create_table(
        "risk_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="SET NULL")),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("severity", sa.String(16), server_default="warning"),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("details", postgresql.JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column("resolved", sa.Boolean, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "event_type IN ('max_loss_hit','daily_drawdown','consecutive_losses',"
            "'position_limit','manual_stop','api_error')",
            name="ck_risk_event_type",
        ),
        sa.CheckConstraint("severity IN ('info','warning','critical')", name="ck_risk_event_severity"),
    )

    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("type", sa.String(48), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("data", postgresql.JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column("read", sa.Boolean, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "type IN ('trade_opened','trade_closed','risk_alert','agent_stopped',"
            "'profit_target','system')",
            name="ck_notification_type",
        ),
    )
    op.create_index("idx_notifications_user_unread", "notifications", ["user_id", "read"])


def downgrade() -> None:
    op.drop_table("notifications")
    op.drop_table("risk_events")
    op.drop_table("agent_performance")
    op.drop_table("positions")
    op.drop_table("trades")
    op.drop_table("agents")
    op.drop_table("strategies")
    op.drop_table("api_keys")
    op.drop_table("users")
