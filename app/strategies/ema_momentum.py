"""EMA Momentum v2.

Filters added on top of the original EMA-21/50/200 alignment trigger:

    * Distance filter: reject if |close - EMA21| > 1.5 ATR (chasing)
    * Streak: 2 consecutive H1 closes in trend direction, both above/below EMA21
    * Range expansion: signal bar range > 1.2 ATR
    * EMA21/50 spread today > spread 10 bars ago
    * Exhaustion guard: RSI 25..75 only
    * H4 alignment is enforced by the pipeline (HTFBiasEngine)
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
from app.strategies.helpers.indicators import compute_rsi


class EMAMomentumStrategy(BaseStrategy):
    name = "ema_momentum"
    required_timeframes = ["H1"]
    min_candles = 220

    DEFAULT_PARAMS: ClassVar[dict[str, float]] = {
        "EMA_FAST": 21,
        "EMA_MID": 50,
        "EMA_SLOW": 200,
        "ATR_LENGTH": 14,
        "MIN_RISK_ATR": 1.5,
        "MIN_SL_PRICE": 8.0,
        "STOP_BUFFER_PTS": 5.0,
        "MAX_DIST_FROM_EMA21_ATR": 1.5,
        "STREAK_BARS": 2,
        "RANGE_EXPANSION_ATR": 1.2,
        "SPREAD_LOOKBACK_BARS": 10,
        "RSI_MAX": 75.0,
        "RSI_MIN": 25.0,
        "TP1_RR": 1.0,
        "TP2_RR": 2.5,
        "BASE_CONFIDENCE": 60,
    }

    def analyze(self, candles: pd.DataFrame) -> list[CandidateSignal]:
        self.validate_data(candles)
        c = candles.copy().reset_index(drop=True)

        ema_f = compute_ema(c["close"], int(self.params["EMA_FAST"]))
        ema_m = compute_ema(c["close"], int(self.params["EMA_MID"]))
        ema_s = compute_ema(c["close"], int(self.params["EMA_SLOW"]))
        atr = compute_atr(c["high"], c["low"], c["close"], length=int(self.params["ATR_LENGTH"]))
        rsi = compute_rsi(c["close"], length=14)

        swing_highs = detect_swing_highs(c["high"], order=5)
        swing_lows = detect_swing_lows(c["low"], order=5)

        opens = c["open"].values
        highs = c["high"].values
        lows = c["low"].values
        closes = c["close"].values
        timestamps = c["timestamp"].values

        n = len(c)
        out: list[CandidateSignal] = []

        for i in range(self.min_candles, n):
            atr_val = float(atr.iloc[i])
            if isnan(atr_val) or atr_val <= 0:
                continue

            ef = float(ema_f.iloc[i])
            em = float(ema_m.iloc[i])
            es = float(ema_s.iloc[i])
            if isnan(ef) or isnan(em) or isnan(es):
                continue

            ts = pd.Timestamp(timestamps[i]).to_pydatetime()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if not is_in_any_major_session(ts):
                continue

            close_i = float(closes[i])
            range_i = float(highs[i]) - float(lows[i])

            # Range expansion
            if range_i < self.params["RANGE_EXPANSION_ATR"] * atr_val:
                continue

            # Spread expansion
            lb = int(self.params["SPREAD_LOOKBACK_BARS"])
            if i < lb:
                continue
            spread_now = abs(ef - em)
            spread_then = abs(float(ema_f.iloc[i - lb]) - float(ema_m.iloc[i - lb]))
            if spread_now <= spread_then:
                continue

            # Distance filter
            if abs(close_i - ef) > self.params["MAX_DIST_FROM_EMA21_ATR"] * atr_val:
                continue

            # RSI exhaustion guard
            rsi_now = float(rsi.iloc[i]) if not isnan(rsi.iloc[i]) else 50.0
            if rsi_now > self.params["RSI_MAX"] or rsi_now < self.params["RSI_MIN"]:
                continue

            # EMA alignment
            bullish = ef > em > es and close_i > ef
            bearish = ef < em < es and close_i < ef
            if not (bullish or bearish):
                continue

            # Streak: STREAK_BARS consecutive directional closes above/below EMA21
            streak = int(self.params["STREAK_BARS"])
            ok = True
            for k in range(streak):
                idx = i - k
                if idx < 1:
                    ok = False
                    break
                ck = float(closes[idx])
                ok_bar = (ck > float(opens[idx])) if bullish else (ck < float(opens[idx]))
                ok_above_ema = (ck > float(ema_f.iloc[idx])) if bullish else (ck < float(ema_f.iloc[idx]))
                if not (ok_bar and ok_above_ema):
                    ok = False
                    break
            if not ok:
                continue

            sig = self._build(
                bullish=bullish, i=i, atr_val=atr_val,
                ef=ef, em=em, es=es,
                opens=opens, highs=highs, lows=lows, closes=closes,
                timestamps=timestamps,
                swing_highs=swing_highs, swing_lows=swing_lows,
                ts=ts,
            )
            if sig is not None:
                out.append(sig)

        return out

    def _build(
        self,
        bullish: bool, i: int, atr_val: float,
        ef: float, em: float, es: float,
        opens: np.ndarray, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
        timestamps: np.ndarray,
        swing_highs: np.ndarray, swing_lows: np.ndarray,
        ts: datetime,
    ) -> CandidateSignal | None:
        entry = float(closes[i])
        cushion = max(
            self.params["MIN_RISK_ATR"] * atr_val,
            self.params["MIN_SL_PRICE"],
            self.params["STOP_BUFFER_PTS"],
        )

        # SL beyond recent swing
        if bullish:
            recent_lows = [
                float(lows[k]) for k in swing_lows
                if k < i and k >= i - 30
            ]
            if not recent_lows:
                recent_lows = [float(np.min(lows[max(0, i - 30): i + 1]))]
            sl_anchor = min(recent_lows)
            sl = sl_anchor - cushion
            risk = entry - sl
        else:
            recent_highs = [
                float(highs[k]) for k in swing_highs
                if k < i and k >= i - 30
            ]
            if not recent_highs:
                recent_highs = [float(np.max(highs[max(0, i - 30): i + 1]))]
            sl_anchor = max(recent_highs)
            sl = sl_anchor + cushion
            risk = sl - entry

        if risk <= 0:
            return None

        if bullish:
            tp1 = entry + self.params["TP1_RR"] * risk
            tp2 = entry + self.params["TP2_RR"] * risk
        else:
            tp1 = entry - self.params["TP1_RR"] * risk
            tp2 = entry - self.params["TP2_RR"] * risk

        confidence = float(self.params["BASE_CONFIDENCE"])
        if abs(em - es) > 2.0 * atr_val:
            confidence += 10
        if abs(ef - em) > 1.0 * atr_val:
            confidence += 10

        active_sessions = get_active_sessions(ts)
        session = active_sessions[0] if active_sessions else None

        direction = Direction.BUY if bullish else Direction.SELL
        reasoning = (
            f"EMA-momentum {direction.value}: EMA21={ef:.2f} EMA50={em:.2f} EMA200={es:.2f} "
            f"(spread expanding), streak confirmed, range-expanded bar. "
            f"Entry {entry:.2f}, SL {sl:.2f}."
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
            risk_reward=Decimal(str(round(self.params["TP1_RR"], 2))),
            confidence=Decimal(str(round(min(confidence, 100.0), 2))),
            reasoning=reasoning,
            timestamp=ts,
            session=session,
        )
