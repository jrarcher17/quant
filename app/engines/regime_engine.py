"""RegimeEngine — classifies the current market into one of four regimes.

Strong Trend       — directional, expanding ADX, low candle overlap.
Compression        — low volatility (<= 30th percentile ATR), high overlap.
Reversal Exhaustion— stretched price extreme but ADX fading.
Chop               — none of the above. All strategies disabled.
"""

from __future__ import annotations

from enum import Enum

import pandas as pd
from loguru import logger

from app.strategies.helpers.indicators import (
    atr_percentile_rank,
    avg_overlap_last_n,
    compute_adx,
    compute_atr,
    compute_ema,
    compute_rsi,
)


class Regime(str, Enum):
    STRONG_TREND = "strong_trend"
    COMPRESSION = "compression"
    EXHAUSTION = "exhaustion"
    CHOP = "chop"
    UNKNOWN = "unknown"


class RegimeEngine:
    # Thresholds tuned for XAUUSD H1
    ADX_TREND = 25.0
    ADX_CHOP = 20.0
    ATR_PCT_COMPRESSION = 0.30
    ATR_PCT_EXHAUSTION = 0.75
    OVERLAP_TREND = 0.55
    OVERLAP_COMPRESSION = 0.65
    OVERLAP_CHOP = 0.75
    EMA_SLOPE_TREND = 0.4   # ATR-normalised slope
    RSI_EXHAUSTION_HIGH = 75.0
    RSI_EXHAUSTION_LOW = 25.0

    def classify(self, h1: pd.DataFrame, h4: pd.DataFrame | None = None) -> Regime:
        if h1.empty or len(h1) < 100:
            return Regime.UNKNOWN

        adx_series = compute_adx(h1["high"], h1["low"], h1["close"], length=14)
        atr_series = compute_atr(h1["high"], h1["low"], h1["close"], length=14)
        ema21 = compute_ema(h1["close"], 21)
        rsi_series = compute_rsi(h1["close"], length=14)

        adx = float(adx_series.dropna().iloc[-1]) if adx_series.dropna().size else 0.0
        atr_pct = atr_percentile_rank(atr_series, lookback=200)
        atr_now = float(atr_series.dropna().iloc[-1]) if atr_series.dropna().size else 1.0
        rsi = float(rsi_series.dropna().iloc[-1]) if rsi_series.dropna().size else 50.0

        # ATR-normalised EMA slope over last 10 bars
        slope = 0.0
        if len(ema21.dropna()) >= 11 and atr_now > 0:
            slope = (float(ema21.iloc[-1]) - float(ema21.iloc[-11])) / atr_now

        overlap = avg_overlap_last_n(h1["high"], h1["low"], n=10)

        regime = Regime.CHOP

        if (
            adx >= self.ADX_TREND
            and abs(slope) >= self.EMA_SLOPE_TREND
            and overlap < self.OVERLAP_TREND
        ):
            regime = Regime.STRONG_TREND
        elif (
            atr_pct <= self.ATR_PCT_COMPRESSION
            and overlap >= self.OVERLAP_COMPRESSION
            and adx < self.ADX_TREND
        ):
            regime = Regime.COMPRESSION
        elif (
            atr_pct >= self.ATR_PCT_EXHAUSTION
            and adx < self.ADX_TREND
            and (rsi >= self.RSI_EXHAUSTION_HIGH or rsi <= self.RSI_EXHAUSTION_LOW)
        ):
            regime = Regime.EXHAUSTION
        elif adx < self.ADX_CHOP or overlap > self.OVERLAP_CHOP:
            regime = Regime.CHOP
        else:
            regime = Regime.CHOP

        logger.debug(
            "RegimeEngine: adx={:.1f} atr_pct={:.2f} slope={:.2f} overlap={:.2f} rsi={:.1f} -> {}",
            adx, atr_pct, slope, overlap, rsi, regime.value,
        )
        return regime
