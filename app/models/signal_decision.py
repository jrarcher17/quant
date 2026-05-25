"""SignalDecision: every accept/reject decision the pipeline makes.

Written for both accepted signals (with the resulting signal_id) and
rejected candidates (with the rejection reason). This is the ground truth
for debugging "why didn't we trade?" or "why did we trade that?" questions.
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SignalDecision(Base):
    __tablename__ = "signal_decisions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    signal_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("signals.id", ondelete="SET NULL"),
        nullable=True,
    )

    strategy_name: Mapped[str] = mapped_column(String(40), nullable=False)
    direction: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    entry_price: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 5), nullable=True
    )

    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    score: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 2), nullable=True)
    score_threshold: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(4, 2), nullable=True
    )

    regime: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    htf_bias: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    session: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    news_blocked: Mapped[bool] = mapped_column(Boolean, default=False)

    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    score_breakdown: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    context_snapshot: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
