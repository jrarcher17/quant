"""Broker order model — tracks every order submitted to the broker (OANDA)."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BrokerOrder(Base):
    """One row per order placed with the broker for a given signal.

    Created immediately after the order request is sent. Updated when the
    order fills (broker_trade_id, fill_price, status → "filled") and again
    when the position is eventually closed (status → "closed").

    If the broker rejects the order, status is set to "error" and
    error_detail contains the rejection reason.
    """

    __tablename__ = "broker_orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("signals.id", ondelete="CASCADE"), index=True
    )
    broker_order_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
    broker_trade_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
    instrument: Mapped[str] = mapped_column(String(20))
    units: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    fill_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 5), nullable=True)
    # "practice" or "live" — recorded so historical orders are labelled correctly
    # even if the mode changes later.
    mode: Mapped[str] = mapped_column(String(16))
    # "pending" | "filled" | "cancelled" | "closed" | "error"
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
