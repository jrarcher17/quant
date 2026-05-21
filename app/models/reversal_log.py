"""Reversal log — tracks stop-and-reverse (SAR) events on paper trades.

One row per reversed trade. The unique constraint on original_trade_id
ensures each paper trade can only trigger one reversal.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Numeric, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ReversalLog(Base):
    __tablename__ = "reversal_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # The paper trade that was closed early and reversed
    original_trade_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("paper_trades.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,  # one reversal per trade
    )

    # The new paper trade opened in the opposite direction
    reversal_trade_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("paper_trades.id", ondelete="SET NULL"),
        nullable=True,
    )

    trigger_price: Mapped[Decimal] = mapped_column(Numeric(12, 5), nullable=False)
    threshold_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    trend_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reversed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
