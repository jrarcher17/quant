"""add paper trading tables

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-05-12 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "paper_account" not in existing_tables:
        op.create_table(
            "paper_account",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("starting_balance", sa.Numeric(12, 2), nullable=False, server_default="1000.00"),
            sa.Column("balance", sa.Numeric(12, 2), nullable=False, server_default="1000.00"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )

    if "paper_trades" not in existing_tables:
        op.create_table(
            "paper_trades",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("signal_id", sa.BigInteger(), nullable=True),
            sa.Column("symbol", sa.String(10), nullable=False),
            sa.Column("direction", sa.String(5), nullable=False),
            sa.Column("initial_units", sa.Numeric(14, 4), nullable=False),
            sa.Column("current_units", sa.Numeric(14, 4), nullable=False),
            sa.Column("risk_amount_usd", sa.Numeric(10, 2), nullable=False),
            sa.Column("entry_price", sa.Numeric(12, 5), nullable=False),
            sa.Column("stop_loss", sa.Numeric(12, 5), nullable=False),
            sa.Column("take_profit_1", sa.Numeric(12, 5), nullable=False),
            sa.Column("take_profit_2", sa.Numeric(12, 5), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="open"),
            sa.Column("tp1_hit", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("tp1_price", sa.Numeric(12, 5), nullable=True),
            sa.Column("tp1_pnl_usd", sa.Numeric(10, 2), nullable=True),
            sa.Column("tp1_hit_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("close_price", sa.Numeric(12, 5), nullable=True),
            sa.Column("close_reason", sa.String(20), nullable=True),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("realized_pnl_usd", sa.Numeric(10, 2), nullable=False, server_default="0.00"),
            sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["signal_id"], ["signals.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_paper_trades_status", "paper_trades", ["status"])
        op.create_index("ix_paper_trades_symbol", "paper_trades", ["symbol"])


def downgrade() -> None:
    op.drop_table("paper_trades")
    op.drop_table("paper_account")
