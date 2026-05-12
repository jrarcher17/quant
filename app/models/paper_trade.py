"""Paper trading account and trade models."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PaperAccount(Base):
    """Single-row paper trading account — tracks running balance."""

    __tablename__ = "paper_account"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    starting_balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("1000.00"))
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("1000.00"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PaperTrade(Base):
    """One row per paper trade opened from a signal."""

    __tablename__ = "paper_trades"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("signals.id", ondelete="SET NULL"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(10))
    direction: Mapped[str] = mapped_column(String(5))  # BUY or SELL

    # Position sizing
    initial_units: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    current_units: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    risk_amount_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2))

    # Prices
    entry_price: Mapped[Decimal] = mapped_column(Numeric(12, 5))
    stop_loss: Mapped[Decimal] = mapped_column(Numeric(12, 5))
    take_profit_1: Mapped[Decimal] = mapped_column(Numeric(12, 5))
    take_profit_2: Mapped[Decimal] = mapped_column(Numeric(12, 5))

    # Status: "open" | "closed" | "cancelled"
    status: Mapped[str] = mapped_column(String(20), default="open")

    # TP1 partial close (50% at TP1, SL moves to breakeven)
    tp1_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    tp1_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 5), nullable=True)
    tp1_pnl_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), nullable=True)
    tp1_hit_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Final close
    close_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 5), nullable=True)
    # close_reason: "tp2" | "sl" | "be" (stop-loss moved to breakeven) | "expired" | "manual"
    close_reason: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Total realised P&L for the full trade (tp1 partial + final close)
    realized_pnl_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"))

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
