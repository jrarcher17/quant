"""add trading_symbol to trade_settings

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-11 19:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trade_settings",
        sa.Column(
            "trading_symbol",
            sa.String(10),
            nullable=False,
            server_default="XAUUSD",
        ),
    )


def downgrade() -> None:
    op.drop_column("trade_settings", "trading_symbol")
