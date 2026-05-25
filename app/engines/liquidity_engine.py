"""LiquidityEngine — major reference levels for stop-hunt and target zones.

Builds a map of:
    PDH/PDL              — previous calendar-day high/low (UTC)
    PWH/PWL              — previous calendar-week high/low
    asia_high/asia_low   — most recent Asian session extremes
    london_high/low      — most recent London session extremes
    swing_highs/lows     — last 5 H1 swing pivots

Used by the scoring engine ("entering into liquidity = penalty") and by
the target engine ("nearest opposing liquidity = TP candidate").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pandas as pd
from loguru import logger

from app.strategies.helpers.swing_detection import (
    detect_swing_highs,
    detect_swing_lows,
)


@dataclass
class LiquidityMap:
    pdh: float | None = None
    pdl: float | None = None
    pwh: float | None = None
    pwl: float | None = None
    asia_high: float | None = None
    asia_low: float | None = None
    london_high: float | None = None
    london_low: float | None = None
    swing_highs: list[float] = field(default_factory=list)
    swing_lows: list[float] = field(default_factory=list)

    def all_levels_above(self, price: float) -> list[float]:
        candidates = [
            v for v in (
                self.pdh, self.pwh, self.asia_high, self.london_high,
                *self.swing_highs,
            )
            if v is not None and v > price
        ]
        return sorted(candidates)

    def all_levels_below(self, price: float) -> list[float]:
        candidates = [
            v for v in (
                self.pdl, self.pwl, self.asia_low, self.london_low,
                *self.swing_lows,
            )
            if v is not None and v < price
        ]
        return sorted(candidates, reverse=True)

    def nearest_obstacle_pts(self, entry: float, direction: str) -> float | None:
        """Distance to the nearest opposing level the trade has to plough through."""
        if direction == "BUY":
            above = self.all_levels_above(entry)
            return None if not above else above[0] - entry
        below = self.all_levels_below(entry)
        return None if not below else entry - below[0]

    def nearest_target_pts(self, entry: float, direction: str) -> float | None:
        """Distance to the nearest level that can act as a TP magnet."""
        return self.nearest_obstacle_pts(entry, direction)

    def as_snapshot(self) -> dict:
        return {
            "pdh": self.pdh, "pdl": self.pdl,
            "pwh": self.pwh, "pwl": self.pwl,
            "asia_high": self.asia_high, "asia_low": self.asia_low,
            "london_high": self.london_high, "london_low": self.london_low,
            "swing_highs": self.swing_highs[-3:] if self.swing_highs else [],
            "swing_lows": self.swing_lows[-3:] if self.swing_lows else [],
        }


class LiquidityEngine:
    """Build a LiquidityMap from H1 + D1 candles."""

    def build_map(self, h1: pd.DataFrame, d1: pd.DataFrame) -> LiquidityMap:
        m = LiquidityMap()
        if h1.empty:
            return m

        now = datetime.now(timezone.utc)

        # PDH / PDL — previous UTC calendar day
        if not d1.empty and len(d1) >= 2:
            prev_day = d1.iloc[-2]  # most recently closed day
            try:
                m.pdh = float(prev_day["high"])
                m.pdl = float(prev_day["low"])
            except Exception:
                pass

        # PWH / PWL — week before this one (Mon..Sun UTC)
        try:
            today = now.date()
            this_monday = today - timedelta(days=today.weekday())
            prev_monday = this_monday - timedelta(days=7)
            prev_sunday = this_monday - timedelta(days=1)
            if not d1.empty:
                d1_ts = pd.to_datetime(d1["timestamp"], utc=True)
                mask = (d1_ts.dt.date >= prev_monday) & (d1_ts.dt.date <= prev_sunday)
                slice_ = d1[mask]
                if not slice_.empty:
                    m.pwh = float(slice_["high"].max())
                    m.pwl = float(slice_["low"].min())
        except Exception:
            logger.debug("LiquidityEngine: PWH/PWL calc failed")

        # Asian + London session extremes — most recent occurrence
        try:
            ts = pd.to_datetime(h1["timestamp"], utc=True)
            today = ts.dt.date.iloc[-1]

            for tries in range(3):  # check today, yesterday, day before
                day = today - timedelta(days=tries)
                day_mask = ts.dt.date == day

                asia_mask = day_mask & (ts.dt.hour >= 0) & (ts.dt.hour < 7)
                if asia_mask.any() and m.asia_high is None:
                    m.asia_high = float(h1.loc[asia_mask, "high"].max())
                    m.asia_low = float(h1.loc[asia_mask, "low"].min())

                london_mask = day_mask & (ts.dt.hour >= 7) & (ts.dt.hour < 12)
                if london_mask.any() and m.london_high is None:
                    m.london_high = float(h1.loc[london_mask, "high"].max())
                    m.london_low = float(h1.loc[london_mask, "low"].min())

                if m.asia_high is not None and m.london_high is not None:
                    break
        except Exception:
            logger.debug("LiquidityEngine: session level calc failed")

        # Swing pivots — last 5 of each on H1
        try:
            highs = detect_swing_highs(h1["high"], order=5)
            lows = detect_swing_lows(h1["low"], order=5)
            m.swing_highs = [float(h1["high"].iloc[i]) for i in highs[-5:]]
            m.swing_lows = [float(h1["low"].iloc[i]) for i in lows[-5:]]
        except Exception:
            logger.debug("LiquidityEngine: swing pivot calc failed")

        return m
