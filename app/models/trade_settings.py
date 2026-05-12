"""User-editable trade risk and target settings."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Numeric, String, func
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
    # Minimum confidence required to fire a signal in the opposite direction
    # of an active position. 100.00 = effectively disabled (very few candidates
    # ever score that high). Lower this (e.g. 80) to enable hedge / reversal
    # plays through the opposite-direction block in SignalPipeline.
    hedge_min_confidence: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("100.00")
    )
    # Multiplier applied to `risk_per_trade_pct` when the candidate is a
    # hedge (opposite-direction override). 0.5 = half the usual risk.
    # Range 0.0..1.0; set 1.0 to size hedges identically to primary signals.
    hedge_risk_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, default=Decimal("0.500")
    )
    # Maximum distance (in XAUUSD pips) between an incoming candidate's
    # entry and an existing active same-direction signal's entry below
    # which the candidate is treated as a duplicate. 0 disables the check
    # and falls back to time-window-only dedup.
    dedup_price_distance_pips: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("30.00")
    )
    # Active trading instrument — overrides the TRADING_SYMBOL env var when set.
    trading_symbol: Mapped[str] = mapped_column(
        String(10), nullable=False, default="XAUUSD"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
