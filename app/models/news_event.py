"""News event calendar — economic releases that may move XAUUSD.

Sourced from ForexFactory weekly XML feed (free public). Refreshed daily
by the news_calendar job.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class NewsEvent(Base):
    __tablename__ = "news_events"
    __table_args__ = (
        UniqueConstraint("event_time", "title", name="uq_news_time_title"),
        Index("ix_news_event_time", "event_time"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    impact: Mapped[str] = mapped_column(String(10), nullable=False)  # high/med/low
    forecast: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    previous: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
