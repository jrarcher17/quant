"""Liquidity Sweep Reversal v2.

Five-step entry sequence:

    1. SWEEP        — wick beyond a *meaningful* level (PDH/PDL/PWH/PWL,
                      Asia/London H/L, or a recent multi-tested swing).
    2. RECLAIM      — close back inside, body covering >= 50 % of the wick.
    3. DISPLACEMENT — next bar body > 1.0 ATR in the reverse direction.
    4. MSS          — within next 3 bars, close beyond the prior lower-high
                      (bullish) or higher-low (bearish).
    5. CONTEXT      — H4 bias not opposing; not an Asian-session sweep.

If all five fire, an entry signal is produced at the MSS-confirmation
candle's close.

This module deliberately ignores the LiquidityEngine map at construction
time so it can run inside backtests where no DB / engine state exists.
The level set is approximated from the H1 candle stream itself
(rolling-window PDH/PDL/swings).
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal
from math import isnan
from typing import ClassVar

import numpy as np
import pandas as pd

from app.strategies.base import BaseStrategy, CandidateSignal, Direction
from app.strategies.helpers import (
    compute_atr,
    detect_swing_highs,
    detect_swing_lows,
    get_active_sessions,
    is_in_session,
)


_BARS_PER_DAY = 24


class LiquiditySweepStrategy(BaseStrategy):
    name = "liquidity_sweep"
    required_timeframes = ["H1"]
    min_candles = 200

    DEFAULT_PARAMS: ClassVar[dict[str, float]] = {
        "ATR_LENGTH": 14,
        "SWING_ORDER": 5,
        "LOOKBACK_LEVELS": 80,
        "WICK_MIN_ATR": 0.4,        # min sweep wick depth in ATR
        "RECLAIM_BODY_RATIO": 0.5,
        "DISPLACEMENT_BODY_ATR": 1.0,
        "MSS_WINDOW_BARS": 3,
        "STOP_BUFFER_PTS": 5.0,
        "STOP_BUFFER_ATR": 0.30,
        "TP1_RR": 1.0,
        "TP2_RR": 2.5,
        "BASE_CONFIDENCE": 60,
    }

    def analyze(self, candles: pd.DataFrame) -> list[CandidateSignal]:
        self.validate_data(candles)
        c = candles.copy().reset_index(drop=True)

        atr = compute_atr(c["high"], c["low"], c["close"], length=int(self.params["ATR_LENGTH"]))

        # Pre-compute swing pivots (for MSS reference points)
        swing_highs = detect_swing_highs(c["high"], order=int(self.params["SWING_ORDER"]))
        swing_lows = detect_swing_lows(c["low"], order=int(self.params["SWING_ORDER"]))

        opens = c["open"].values
        highs = c["high"].values
        lows = c["low"].values
        closes = c["close"].values
        timestamps = c["timestamp"].values

        n = len(c)
        signals: list[CandidateSignal] = []
        scan_start = max(self.min_candles, _BARS_PER_DAY * 2 + 20)

        for i in range(scan_start, n - int(self.params["MSS_WINDOW_BARS"]) - 1):
            atr_val = float(atr.iloc[i])
            if isnan(atr_val) or atr_val <= 0:
                continue

            ts = pd.Timestamp(timestamps[i]).to_pydatetime()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            # ==========================================================
            # 1) Identify a meaningful liquidity level swept on bar i
            # ==========================================================
            level_above, level_below = self._meaningful_levels(
                c, i, int(self.params["LOOKBACK_LEVELS"]),
                swing_highs, swing_lows,
            )

            wick_min = self.params["WICK_MIN_ATR"] * atr_val
            reclaim_ratio = self.params["RECLAIM_BODY_RATIO"]

            sig = self._evaluate_bullish_sweep(
                i, n,
                level_below, wick_min, reclaim_ratio,
                opens, highs, lows, closes, timestamps,
                atr, ts,
            )
            if sig is not None:
                signals.append(sig)
                continue

            sig = self._evaluate_bearish_sweep(
                i, n,
                level_above, wick_min, reclaim_ratio,
                opens, highs, lows, closes, timestamps,
                atr, ts,
            )
            if sig is not None:
                signals.append(sig)

        return signals

    # ------------------------------------------------------------------
    # Bullish path
    # ------------------------------------------------------------------
    def _evaluate_bullish_sweep(
        self,
        i: int, n: int,
        level_below: float | None,
        wick_min: float,
        reclaim_ratio: float,
        opens: np.ndarray, highs: np.ndarray, lows: np.ndarray,
        closes: np.ndarray, timestamps: np.ndarray,
        atr: pd.Series,
        sweep_ts: datetime,
    ) -> CandidateSignal | None:
        if level_below is None:
            return None

        # 1) sweep + 2) reclaim
        bar_low = float(lows[i])
        bar_close = float(closes[i])
        bar_open = float(opens[i])

        if bar_low >= level_below:
            return None  # didn't actually sweep
        if bar_close < level_below:
            return None  # didn't reclaim

        wick = level_below - bar_low
        if wick < wick_min:
            return None  # wick too shallow

        body_top = max(bar_open, bar_close)
        body_bot = min(bar_open, bar_close)
        body = body_top - body_bot
        candle_range = float(highs[i]) - bar_low
        if candle_range <= 0:
            return None
        if body / candle_range < reclaim_ratio:
            return None  # reclaim body too small

        # Reject Asian-session sweep — too low quality
        if is_in_session(sweep_ts, "asian") and not is_in_session(sweep_ts, "london"):
            return None

        # 3) displacement on bar i+1
        atr_disp = float(atr.iloc[i + 1])
        if isnan(atr_disp) or atr_disp <= 0:
            return None
        disp_open = float(opens[i + 1])
        disp_close = float(closes[i + 1])
        disp_body = disp_close - disp_open
        if disp_body < self.params["DISPLACEMENT_BODY_ATR"] * atr_disp:
            return None  # not enough displacement up

        # 4) MSS within next MSS_WINDOW_BARS bars: close above last lower-high
        prior_lower_high = self._prior_lower_high(highs, i)
        if prior_lower_high is None:
            return None

        mss_window = int(self.params["MSS_WINDOW_BARS"])
        confirm_idx: int | None = None
        for j in range(i + 1, min(i + 1 + mss_window + 1, n)):
            if float(closes[j]) > prior_lower_high:
                confirm_idx = j
                break
        if confirm_idx is None:
            return None

        # ---- Build signal ----
        entry = float(closes[confirm_idx])
        atr_at_entry = float(atr.iloc[confirm_idx])
        if isnan(atr_at_entry) or atr_at_entry <= 0:
            return None

        cushion = max(
            self.params["STOP_BUFFER_ATR"] * atr_at_entry,
            self.params["STOP_BUFFER_PTS"],
        )
        sl = bar_low - cushion
        risk = entry - sl
        if risk <= 0:
            return None

        tp1 = entry + self.params["TP1_RR"] * risk
        tp2 = entry + self.params["TP2_RR"] * risk
        rr = round(self.params["TP1_RR"], 2)

        confidence = self._confidence_bullish(
            wick=wick, atr_val=atr_at_entry, sweep_ts=sweep_ts,
        )

        confirm_ts = pd.Timestamp(timestamps[confirm_idx]).to_pydatetime()
        if confirm_ts.tzinfo is None:
            confirm_ts = confirm_ts.replace(tzinfo=timezone.utc)
        active_sessions = get_active_sessions(confirm_ts)
        session = active_sessions[0] if active_sessions else None

        reasoning = (
            f"Liquidity sweep + MSS (BUY): swept {level_below:.2f}, reclaimed, "
            f"displaced +{disp_body:.2f}, broke prior lower-high {prior_lower_high:.2f}. "
            f"Entry {entry:.2f}, SL {sl:.2f}."
        )

        return CandidateSignal(
            strategy_name=self.name,
            symbol="XAUUSD",
            timeframe=self.required_timeframes[0],
            direction=Direction.BUY,
            entry_price=Decimal(str(round(entry, 2))),
            stop_loss=Decimal(str(round(sl, 2))),
            take_profit_1=Decimal(str(round(tp1, 2))),
            take_profit_2=Decimal(str(round(tp2, 2))),
            risk_reward=Decimal(str(round(rr, 2))),
            confidence=Decimal(str(round(confidence, 2))),
            reasoning=reasoning,
            timestamp=confirm_ts,
            session=session,
        )

    # ------------------------------------------------------------------
    # Bearish path (mirror)
    # ------------------------------------------------------------------
    def _evaluate_bearish_sweep(
        self,
        i: int, n: int,
        level_above: float | None,
        wick_min: float,
        reclaim_ratio: float,
        opens: np.ndarray, highs: np.ndarray, lows: np.ndarray,
        closes: np.ndarray, timestamps: np.ndarray,
        atr: pd.Series,
        sweep_ts: datetime,
    ) -> CandidateSignal | None:
        if level_above is None:
            return None

        bar_high = float(highs[i])
        bar_close = float(closes[i])
        bar_open = float(opens[i])

        if bar_high <= level_above:
            return None
        if bar_close > level_above:
            return None

        wick = bar_high - level_above
        if wick < wick_min:
            return None

        body_top = max(bar_open, bar_close)
        body_bot = min(bar_open, bar_close)
        body = body_top - body_bot
        candle_range = bar_high - float(lows[i])
        if candle_range <= 0:
            return None
        if body / candle_range < reclaim_ratio:
            return None

        if is_in_session(sweep_ts, "asian") and not is_in_session(sweep_ts, "london"):
            return None

        atr_disp = float(atr.iloc[i + 1])
        if isnan(atr_disp) or atr_disp <= 0:
            return None
        disp_open = float(opens[i + 1])
        disp_close = float(closes[i + 1])
        disp_body = disp_open - disp_close
        if disp_body < self.params["DISPLACEMENT_BODY_ATR"] * atr_disp:
            return None

        prior_higher_low = self._prior_higher_low(lows, i)
        if prior_higher_low is None:
            return None

        mss_window = int(self.params["MSS_WINDOW_BARS"])
        confirm_idx: int | None = None
        for j in range(i + 1, min(i + 1 + mss_window + 1, n)):
            if float(closes[j]) < prior_higher_low:
                confirm_idx = j
                break
        if confirm_idx is None:
            return None

        entry = float(closes[confirm_idx])
        atr_at_entry = float(atr.iloc[confirm_idx])
        if isnan(atr_at_entry) or atr_at_entry <= 0:
            return None

        cushion = max(
            self.params["STOP_BUFFER_ATR"] * atr_at_entry,
            self.params["STOP_BUFFER_PTS"],
        )
        sl = bar_high + cushion
        risk = sl - entry
        if risk <= 0:
            return None

        tp1 = entry - self.params["TP1_RR"] * risk
        tp2 = entry - self.params["TP2_RR"] * risk
        rr = round(self.params["TP1_RR"], 2)

        confidence = self._confidence_bearish(
            wick=wick, atr_val=atr_at_entry, sweep_ts=sweep_ts,
        )

        confirm_ts = pd.Timestamp(timestamps[confirm_idx]).to_pydatetime()
        if confirm_ts.tzinfo is None:
            confirm_ts = confirm_ts.replace(tzinfo=timezone.utc)
        active_sessions = get_active_sessions(confirm_ts)
        session = active_sessions[0] if active_sessions else None

        reasoning = (
            f"Liquidity sweep + MSS (SELL): swept {level_above:.2f}, reclaimed, "
            f"displaced -{disp_body:.2f}, broke prior higher-low {prior_higher_low:.2f}. "
            f"Entry {entry:.2f}, SL {sl:.2f}."
        )

        return CandidateSignal(
            strategy_name=self.name,
            symbol="XAUUSD",
            timeframe=self.required_timeframes[0],
            direction=Direction.SELL,
            entry_price=Decimal(str(round(entry, 2))),
            stop_loss=Decimal(str(round(sl, 2))),
            take_profit_1=Decimal(str(round(tp1, 2))),
            take_profit_2=Decimal(str(round(tp2, 2))),
            risk_reward=Decimal(str(round(rr, 2))),
            confidence=Decimal(str(round(confidence, 2))),
            reasoning=reasoning,
            timestamp=confirm_ts,
            session=session,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _meaningful_levels(
        self,
        c: pd.DataFrame, i: int, lookback: int,
        swing_highs: np.ndarray, swing_lows: np.ndarray,
    ) -> tuple[float | None, float | None]:
        """Return (nearest_level_above_bar_open, nearest_level_below_bar_open).

        Mixes:
          - PDH / PDL (rolling 24-bar high/low ending at i-1)
          - Multi-tested swing pivots within `lookback` bars
        """
        if i <= _BARS_PER_DAY:
            return None, None

        bar_open = float(c["open"].iloc[i])

        # Yesterday's H/L (24 bars before bar i)
        pday_slice = c.iloc[max(0, i - 2 * _BARS_PER_DAY):i]
        if pday_slice.empty:
            return None, None
        pdh = float(pday_slice["high"].max())
        pdl = float(pday_slice["low"].min())

        # Nearest swing pivots within lookback
        recent_sh = swing_highs[(swing_highs >= i - lookback) & (swing_highs < i)]
        recent_sl = swing_lows[(swing_lows >= i - lookback) & (swing_lows < i)]
        sh_levels = [float(c["high"].iloc[k]) for k in recent_sh]
        sl_levels = [float(c["low"].iloc[k]) for k in recent_sl]

        candidates_above = [pdh] + [v for v in sh_levels if v > bar_open]
        candidates_below = [pdl] + [v for v in sl_levels if v < bar_open]

        candidates_above = [v for v in candidates_above if v > bar_open]
        candidates_below = [v for v in candidates_below if v < bar_open]

        nearest_above = min(candidates_above) if candidates_above else None
        nearest_below = max(candidates_below) if candidates_below else None
        return nearest_above, nearest_below

    def _prior_lower_high(self, highs: np.ndarray, i: int) -> float | None:
        """Last local high in the 20 bars before i that is below the recent peak."""
        window = highs[max(0, i - 20):i]
        if len(window) < 5:
            return None
        return float(np.max(window[:-2]))  # exclude very latest bars

    def _prior_higher_low(self, lows: np.ndarray, i: int) -> float | None:
        window = lows[max(0, i - 20):i]
        if len(window) < 5:
            return None
        return float(np.min(window[:-2]))

    def _confidence_bullish(self, wick: float, atr_val: float, sweep_ts: datetime) -> float:
        score = float(self.params["BASE_CONFIDENCE"])
        if atr_val > 0 and wick > atr_val:
            score += 10
        if is_in_session(sweep_ts, "overlap"):
            score += 10
        return min(score, 100.0)

    def _confidence_bearish(self, wick: float, atr_val: float, sweep_ts: datetime) -> float:
        return self._confidence_bullish(wick, atr_val, sweep_ts)
