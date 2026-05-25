"""NewsEngine — blocks new trades around high-impact economic releases.

Currencies/events that move XAUUSD: USD (Fed, CPI, NFP, FOMC, Powell,
ISM, retail sales, PPI), DXY-affecting EUR/JPY/GBP releases of high
impact, and geopolitical risk events (manually added).

Sourced from ForexFactory weekly XML feed (free) by news_calendar.py;
this engine only reads the table.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news_event import NewsEvent


# Pre-event blackout (minutes) and post-event blackout (minutes)
_PRE_BLOCK = 30
_POST_BLOCK = 15

# Currencies whose high-impact news materially moves XAUUSD
_RELEVANT_CCY = {"USD", "EUR", "GBP", "JPY", "CHF", "ALL"}


@dataclass
class NewsWindow:
    blocked: bool
    reason: str | None = None
    next_event_at: datetime | None = None
    next_event_title: str | None = None


class NewsEngine:
    async def window(self, session: AsyncSession, now: datetime) -> NewsWindow:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        lookback = now - timedelta(minutes=_POST_BLOCK)
        lookahead = now + timedelta(hours=12)

        stmt = (
            select(NewsEvent)
            .where(NewsEvent.event_time >= lookback, NewsEvent.event_time <= lookahead)
            .order_by(NewsEvent.event_time)
        )
        events = (await session.execute(stmt)).scalars().all()

        if not events:
            return NewsWindow(blocked=False)

        block_event: NewsEvent | None = None
        next_event: NewsEvent | None = None

        for ev in events:
            ev_time = ev.event_time
            if ev_time.tzinfo is None:
                ev_time = ev_time.replace(tzinfo=timezone.utc)

            if ev.impact.lower() != "high":
                continue
            if ev.currency.upper() not in _RELEVANT_CCY:
                continue

            pre = ev_time - timedelta(minutes=_PRE_BLOCK)
            post = ev_time + timedelta(minutes=_POST_BLOCK)

            if pre <= now <= post:
                block_event = ev
                break
            if ev_time > now and next_event is None:
                next_event = ev

        if block_event is not None:
            ev_time = block_event.event_time
            if ev_time.tzinfo is None:
                ev_time = ev_time.replace(tzinfo=timezone.utc)
            reason = (
                f"news blackout: {block_event.currency} {block_event.title} "
                f"@ {ev_time.strftime('%H:%M UTC')}"
            )
            logger.info("NewsEngine: blocking signals — {}", reason)
            return NewsWindow(
                blocked=True,
                reason=reason,
                next_event_at=ev_time,
                next_event_title=block_event.title,
            )

        return NewsWindow(
            blocked=False,
            next_event_at=next_event.event_time if next_event else None,
            next_event_title=next_event.title if next_event else None,
        )
