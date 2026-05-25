"""Trend Continuation v2.

Pullback-to-EMA-50 reentry, with substantially stricter filters:

    * ADX gate (>= 22, rising 3 bars)
    * EMA-50/200 spread expanding vs 5 bars ago (trend strengthening)
    * Rejection candle at the EMA-50 zone (pin / engulfing pattern)
    * Volume on confirmation candle >= 1.2 * 20-bar avg (when volume present)
    * Stop-entry at confirmation_high + 0.1 ATR (or _low - 0.1 ATR)
    * Exhaustion guard: ATR not declining 3 bars; RSI 22 < x < 78
    * H4 alignment is checked downstream by the pipeline (HTFBiasEngine)
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from math import isnan
from typing import ClassVar

import numpy as np
import pandas as pd

from app.strategies.base import BaseStrategy, CandidateSignal, Direction
from app.strategies.helpers import (
    compute_atr,
    compute_ema,
    detect_swing_highs,
    detect_swing_lows,
    get_active_sessions,
    is_in_any_major_session,
)
from app.strategies.helpers.indicators import compute_adx, compute_rsi


class TrendContinuationStrategy(BaseStrategy):
    name = "trend_continuation"
    required_timeframes = ["H1"]
    min_candles = 220

    DEFAULT_PARAMS: ClassVar[dict[str, float]] = {
        "EMA_FAST": 50,
        "EMA_SLOW": 200,
        "ATR_LENGTH": 14,
        "PULLBACK_ATR_MULT": 1.5,
        "MIN_RISK_ATR": 1.5,
        "MIN_SL_PRICE": 8.0,
        "STOP_BUFFER_PTS": 5.0,
        "TP1_RR": 1.0,
        "TP2_RR": 2.5,
        "ADX_MIN": 22.0,
        "ADX_RISING_BARS": 3,
        "RSI_MAX": 78.0,
        "RSI_MIN": 22.0,
        "VOLUME_MULT": 1.2,
        "STOP_ENTRY_BUFFER_ATR": 0.10,
        "BASE_CONFIDENCE": 60,
    }

    def analyze(self, candles: pd.DataFrame) -> list[CandidateSignal]:
        self.validate_data(candles)
        c = candles.copy().reset_index(drop=True)

        ema_50 = compute_ema(c["close"], int(self.params["EMA_FAST"]))
        ema_200 = compute_ema(c["close"], int(self.params["EMA_SLOW"]))
        atr = compute_atr(c["high"], c["low"], c["close"], length=int(self.params["ATR_LENGTH"]))
        adx = compute_adx(c["high"], c["low"], c["close"], length=14)
        rsi = compute_rsi(c["close"], length=14)

        opens = c["open"].values
        highs = c["high"].values
        lows = c["low"].values
        closes = c["close"].values
        timestamps = c["timestamp"].values

        has_volume = (
            "volume" in c.columns and not c["volume"].fillna(0).eq(0).all()
        )
        volumes = c["volume"].values if has_volume else None

        n = len(c)
        out: list[CandidateSignal] = []

        for i in range(self.min_candles, n - 1):
            atr_val = float(atr.iloc[i])
            if isnan(atr_val) or atr_val <= 0:
                continue

            e50 = float(ema_50.iloc[i])
            e200 = float(ema_200.iloc[i])
            if isnan(e50) or isnan(e200):
                continue

            ts = pd.Timestamp(timestamps[i]).to_pydatetime()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if not is_in_any_major_session(ts):
                continue

            # Trend filter: clear EMA separation
            spread_now = abs(e50 - e200)
            if spread_now < 0.4 * atr_val:
                continue

            bullish = e50 > e200
            bearish = e50 < e200
            if not (bullish or bearish):
                continue

            # Spread expansion: now > 5 bars ago
            e50_prev = float(ema_50.iloc[i - 5])
            e200_prev = float(ema_200.iloc[i - 5])
            spread_prev = abs(e50_prev - e200_prev)
            if spread_now <= spread_prev:
                continue

            # ADX gate
            adx_now = float(adx.iloc[i])
            if isnan(adx_now) or adx_now < self.params["ADX_MIN"]:
                continue
            if not self._adx_rising(adx, i, int(self.params["ADX_RISING_BARS"])):
                continue

            # Exhaustion guard: ATR not declining over last 3 bars
            atr_recent = atr.iloc[max(0, i - 3): i + 1].dropna().to_list()
            if len(atr_recent) >= 4 and atr_recent[-1] < atr_recent[0] * 0.9:
                continue

            rsi_now = float(rsi.iloc[i]) if not isnan(rsi.iloc[i]) else 50.0
            if rsi_now > self.params["RSI_MAX"] or rsi_now < self.params["RSI_MIN"]:
                continue

            # Pullback into the EMA50 zone
            pb_dist = self.params["PULLBACK_ATR_MULT"] * atr_val
            close_i = float(closes[i])

            if bullish:
                if not (e50 - pb_dist <= close_i <= e50 + pb_dist):
                    continue
                rejection = self._is_bullish_rejection_candle(opens, highs, lows, closes, i)
                if not rejection:
                    continue
                sig = self._build_signal(
                    direction=Direction.BUY,
                    confirm_i=i,
                    e50=e50, e200=e200,
                    atr_val=atr_val,
                    opens=opens, highs=highs, lows=lows, closes=closes,
                    timestamps=timestamps,
                    volumes=volumes, has_volume=has_volume,
                )
            else:
                if not (e50 - pb_dist <= close_i <= e50 + pb_dist):
                    continue
                rejection = self._is_bearish_rejection_candle(opens, highs, lows, closes, i)
                if not rejection:
                    continue
                sig = self._build_signal(
                    direction=Direction.SELL,
                    confirm_i=i,
                    e50=e50, e200=e200,
                    atr_val=atr_val,
                    opens=opens, highs=highs, lows=lows, closes=closes,
                    timestamps=timestamps,
                    volumes=volumes, has_volume=has_volume,
                )

            if sig is not None:
                out.append(sig)

        return out

    # ------------------------------------------------------------------
    def _adx_rising(self, adx: pd.Series, i: int, bars: int) -> bool:
        if i < bars:
            return False
        recent = adx.iloc[i - bars: i + 1].dropna().to_list()
        if len(recent) < bars:
            return False
        return all(recent[k] >= recent[k - 1] for k in range(1, len(recent)))

    def _is_bullish_rejection_candle(
        self, opens: np.ndarray, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, i: int
    ) -> bool:
        o, h, l, cl = float(opens[i]), float(highs[i]), float(lows[i]), float(closes[i])
        rng = h - l
        if rng <= 0:
            return False
        body = abs(cl - o)
        lower_wick = min(o, cl) - l
        # Pin bar: lower wick >= 60% of range, body small
        pin = lower_wick >= 0.6 * rng and body <= 0.35 * rng and cl > o
        # Bullish engulfing
        if i == 0:
            engulf = False
        else:
            prev_o, prev_c = float(opens[i - 1]), float(closes[i - 1])
            engulf = prev_c < prev_o and cl > o and cl >= prev_o and o <= prev_c
        return pin or engulf

    def _is_bearish_rejection_candle(
        self, opens: np.ndarray, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, i: int
    ) -> bool:
        o, h, l, cl = float(opens[i]), float(highs[i]), float(lows[i]), float(closes[i])
        rng = h - l
        if rng <= 0:
            return False
        body = abs(cl - o)
        upper_wick = h - max(o, cl)
        pin = upper_wick >= 0.6 * rng and body <= 0.35 * rng and cl < o
        if i == 0:
            engulf = False
        else:
            prev_o, prev_c = float(opens[i - 1]), float(closes[i - 1])
            engulf = prev_c > prev_o and cl < o and cl <= prev_o and o >= prev_c
        return pin or engulf

    def _build_signal(
        self,
        direction: Direction,
        confirm_i: int,
        e50: float, e200: float,
        atr_val: float,
        opens: np.ndarray, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
        timestamps: np.ndarray,
        volumes: np.ndarray | None, has_volume: bool,
    ) -> CandidateSignal | None:
        # Stop-entry: order placed at confirmation_high + buf (BUY) or _low - buf (SELL)
        buf = self.params["STOP_ENTRY_BUFFER_ATR"] * atr_val
        if direction == Direction.BUY:
            entry = float(highs[confirm_i]) + buf
        else:
            entry = float(lows[confirm_i]) - buf

        # Volume confirmation if available
        if has_volume and volumes is not None and confirm_i >= 20:
            avg_vol = float(np.mean(volumes[confirm_i - 20: confirm_i]))
            if avg_vol > 0 and float(volumes[confirm_i]) < self.params["VOLUME_MULT"] * avg_vol:
                return None

        # Stop placement: beyond pullback extreme + buffer
        cushion = max(
            self.params["MIN_RISK_ATR"] * atr_val,
            self.params["MIN_SL_PRICE"],
            self.params["STOP_BUFFER_PTS"],
        )
        if direction == Direction.BUY:
            sl = float(lows[confirm_i]) - cushion
            risk = entry - sl
        else:
            sl = float(highs[confirm_i]) + cushion
            risk = sl - entry

        if risk <= 0:
            return None

        if direction == Direction.BUY:
            tp1 = entry + self.params["TP1_RR"] * risk
            tp2 = entry + self.params["TP2_RR"] * risk
        else:
            tp1 = entry - self.params["TP1_RR"] * risk
            tp2 = entry - self.params["TP2_RR"] * risk

        rr = round(self.params["TP1_RR"], 2)

        confidence = float(self.params["BASE_CONFIDENCE"])
        if abs(e50 - e200) > 1.0 * atr_val:
            confidence += 10

        ts = pd.Timestamp(timestamps[confirm_i]).to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        active_sessions = get_active_sessions(ts)
        session = active_sessions[0] if active_sessions else None

        reasoning = (
            f"Trend-cont {direction.value}: EMA50 {e50:.2f} vs EMA200 {e200:.2f} "
            f"(spread expanding), ADX gated, rejection candle at pullback. "
            f"Stop-entry {entry:.2f}, SL {sl:.2f}."
        )

        return CandidateSignal(
            strategy_name=self.name,
            symbol="XAUUSD",
            timeframe=self.required_timeframes[0],
            direction=direction,
            entry_price=Decimal(str(round(entry, 2))),
            stop_loss=Decimal(str(round(sl, 2))),
            take_profit_1=Decimal(str(round(tp1, 2))),
            take_profit_2=Decimal(str(round(tp2, 2))),
            risk_reward=Decimal(str(round(rr, 2))),
            confidence=Decimal(str(round(min(confidence, 100.0), 2))),
            reasoning=reasoning,
            timestamp=ts,
            session=session,
        )
