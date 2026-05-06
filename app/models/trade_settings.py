"""User-editable trade risk and target settings."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Numeric, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TradeSettings(Base):
    """Singleton settings row used by signal generation and risk checks."""

    __tablename__ = "trade_settings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, default=1)
    risk_per_trade_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), nullable=False, default=Decimal("0.0100")
    )
    max_sl_pips: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("800.00")
    )
    tp1_rr: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("1.50")
    )
    tp2_rr: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("3.00")
    )
    min_risk_reward: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("1.30")
    )
    min_confidence: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("40.00")
    )
    max_concurrent_signals: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=3
    )
    daily_loss_limit_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), nullable=False, default=Decimal("0.0200")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
