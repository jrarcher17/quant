"""NewsCalendar — fetches the ForexFactory weekly XML feed.

ForexFactory publishes a free public weekly calendar at:
    https://nfs.faireconomy.media/ff_calendar_thisweek.xml

Each entry contains: title, country (currency), date, time, impact,
forecast, previous. We upsert every entry into the news_events table;
the NewsEngine reads from there to decide whether to block trading.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import httpx
from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news_event import NewsEvent


_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
_NEXT_WEEK_URL = "https://nfs.faireconomy.media/ff_calendar_nextweek.xml"
_TIMEOUT = 20.0


def _parse_eastern_time(date_str: str, time_str: str) -> datetime | None:
    """ForexFactory publishes times as US Eastern. We convert to UTC.

    date format: "MM-DD-YYYY"; time can be "HH:MMam/pm", "All Day", or "Tentative".
    """
    if not date_str or not time_str:
        return None
    if not re.match(r"^\d{1,2}:\d{2}(am|pm)$", time_str.strip().lower()):
        return None  # skip "All Day", "Tentative", empty, etc.

    try:
        # America/New_York handles DST; we approximate with fixed offset table.
        # For accuracy we use zoneinfo.
        from zoneinfo import ZoneInfo

        dt_naive = datetime.strptime(
            f"{date_str} {time_str}", "%m-%d-%Y %I:%M%p"
        )
        eastern = dt_naive.replace(tzinfo=ZoneInfo("America/New_York"))
        return eastern.astimezone(timezone.utc)
    except Exception:
        return None


async def _fetch_feed(url: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, headers={"User-Agent": "ApexQ/1.0"})
        resp.raise_for_status()
        body = resp.text

    events: list[dict] = []
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        logger.warning("NewsCalendar: feed XML parse failed")
        return events

    for ev in root.findall("event"):
        title = (ev.findtext("title") or "").strip()
        country = (ev.findtext("country") or "").strip().upper()
        impact = (ev.findtext("impact") or "").strip().lower()
        date_str = (ev.findtext("date") or "").strip()
        time_str = (ev.findtext("time") or "").strip().lower()
        forecast = (ev.findtext("forecast") or "").strip() or None
        previous = (ev.findtext("previous") or "").strip() or None

        when = _parse_eastern_time(date_str, time_str)
        if when is None:
            continue
        if not title or not country:
            continue

        events.append(
            {
                "title": title,
                "currency": country,
                "impact": impact or "low",
                "event_time": when,
                "forecast": forecast,
                "previous": previous,
            }
        )
    return events


async def refresh_news_calendar(session: AsyncSession) -> int:
    """Fetch this-week and next-week feeds and upsert into news_events.

    Returns the total number of events stored.
    """
    all_events: list[dict] = []
    for url in (_FEED_URL, _NEXT_WEEK_URL):
        try:
            evs = await _fetch_feed(url)
            logger.info("NewsCalendar: fetched {} events from {}", len(evs), url)
            all_events.extend(evs)
        except Exception:
            logger.exception("NewsCalendar: feed fetch failed for {}", url)

    if not all_events:
        return 0

    # Drop events that are already past — keep DB tidy
    now = datetime.now(timezone.utc)
    fresh = [e for e in all_events if e["event_time"] >= now]

    # Wipe future events for these dates and re-insert (simpler than per-row upsert)
    earliest = min(e["event_time"] for e in fresh)
    await session.execute(
        delete(NewsEvent).where(NewsEvent.event_time >= earliest)
    )
    await session.flush()

    # Avoid unique-constraint duplicates within a single batch
    seen: set[tuple[datetime, str]] = set()
    inserted = 0
    for e in fresh:
        key = (e["event_time"], e["title"])
        if key in seen:
            continue
        seen.add(key)
        session.add(NewsEvent(**e))
        inserted += 1

    await session.commit()
    logger.info("NewsCalendar: upserted {} future events", inserted)
    return inserted


async def is_calendar_stale(session: AsyncSession, max_age_hours: int = 36) -> bool:
    """Return True if the most recent fetch is older than max_age_hours."""
    stmt = select(NewsEvent.fetched_at).order_by(NewsEvent.fetched_at.desc()).limit(1)
    last = (await session.execute(stmt)).scalar_one_or_none()
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - last).total_seconds() / 3600.0
    return age > max_age_hours
