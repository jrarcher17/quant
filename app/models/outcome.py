"""Signal outcome tracking model."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Outcome(Base):
    __tablename__ = "outcomes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("signals.id"), unique=True
    )
    result: Mapped[str] = mapped_column(String(20))  # tp1_hit, tp2_hit, sl_hit, expired
    exit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    pnl_pips: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    duration_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    mae_pts: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), nullable=True)
    mfe_pts: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), nullable=True)
    rr_achieved: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 3), nullable=True)
    regime_at_entry: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    score_at_entry: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 2), nullable=True)
    session_at_entry: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
