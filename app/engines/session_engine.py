"""SessionEngine — quality grading for the current UTC time window.

Lower-quality sessions (late NY, dead Asia, weekend) reduce signal scores;
the lowest tier blocks signal generation entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


# UTC hours -> (label, multiplier)
_SESSION_TABLE = [
    # (start_h, end_h, label, multiplier)
    (12, 13, "ny_pre_open", 1.0),
    (13, 16, "london_ny_overlap", 1.0),
    (7, 12, "london", 0.85),
    (16, 20, "late_ny", 0.55),
    (0, 7, "asia", 0.30),
    (20, 24, "off_hours", 0.0),
]


@dataclass
class SessionInfo:
    label: str
    quality_multiplier: float
    blocked: bool
    weekday: int  # 0 = Monday
    hour: int


class SessionEngine:
    def classify(self, ts: datetime) -> SessionInfo:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        weekday = ts.weekday()  # 0..6
        hour = ts.hour

        # Block weekends entirely (gold market closed Sat 21:00 UTC -> Sun 22:00 UTC)
        if weekday == 5 or weekday == 6:
            return SessionInfo(
                label="weekend",
                quality_multiplier=0.0,
                blocked=True,
                weekday=weekday,
                hour=hour,
            )

        for start, end, label, mult in _SESSION_TABLE:
            if start <= hour < end:
                return SessionInfo(
                    label=label,
                    quality_multiplier=mult,
                    blocked=(mult <= 0.0),
                    weekday=weekday,
                    hour=hour,
                )

        return SessionInfo(
            label="unknown",
            quality_multiplier=0.0,
            blocked=True,
            weekday=weekday,
            hour=hour,
        )
