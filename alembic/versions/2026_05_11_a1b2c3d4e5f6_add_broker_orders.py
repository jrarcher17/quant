"""add broker_orders table

Revision ID: a1b2c3d4e5f6
Revises: 5bdb871466f7
Create Date: 2026-05-11 19:46:00.000000+00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "5bdb871466f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if "broker_orders" not in existing_tables:
        op.create_table(
            "broker_orders",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("signal_id", sa.BigInteger(), nullable=False),
            sa.Column("broker_order_id", sa.String(length=64), nullable=True),
            sa.Column("broker_trade_id", sa.String(length=64), nullable=True),
            sa.Column("instrument", sa.String(length=20), nullable=False),
            sa.Column("units", sa.Numeric(precision=12, scale=4), nullable=False),
            sa.Column("fill_price", sa.Numeric(precision=12, scale=5), nullable=True),
            sa.Column("mode", sa.String(length=16), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("error_detail", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["signal_id"], ["signals.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
        )

    existing_indexes = {idx["name"] for idx in inspector.get_indexes("broker_orders")} if "broker_orders" in existing_tables else set()
    if "ix_broker_orders_signal_id" not in existing_indexes:
        op.create_index("ix_broker_orders_signal_id", "broker_orders", ["signal_id"])
    if "ix_broker_orders_broker_order_id" not in existing_indexes:
        op.create_index("ix_broker_orders_broker_order_id", "broker_orders", ["broker_order_id"])
    if "ix_broker_orders_broker_trade_id" not in existing_indexes:
        op.create_index("ix_broker_orders_broker_trade_id", "broker_orders", ["broker_trade_id"])


def downgrade() -> None:
    op.drop_index("ix_broker_orders_broker_trade_id", table_name="broker_orders")
    op.drop_index("ix_broker_orders_broker_order_id", table_name="broker_orders")
    op.drop_index("ix_broker_orders_signal_id", table_name="broker_orders")
    op.drop_table("broker_orders")
