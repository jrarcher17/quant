"""HTFBiasEngine — H4 + D1 directional bias for trade-direction filtering.

LONG_ONLY / SHORT_ONLY  — only trade in this direction.
LEAN_LONG / LEAN_SHORT  — light preference, opposite direction allowed
                          but penalised in the scoring engine.
NEUTRAL                 — block all trades (HTF unclear).
"""

from __future__ import annotations

from enum import Enum

import pandas as pd
from loguru import logger

from app.strategies.helpers.indicators import compute_ema


class HTFBias(str, Enum):
    LONG_ONLY = "long_only"
    SHORT_ONLY = "short_only"
    LEAN_LONG = "lean_long"
    LEAN_SHORT = "lean_short"
    NEUTRAL = "neutral"


class HTFBiasEngine:
    """Reads H4 + D1 candles and returns a directional bias label."""

    def bias(self, h4: pd.DataFrame, d1: pd.DataFrame) -> HTFBias:
        h4_long, h4_short = self._h4_bias(h4)
        d1_long, d1_short = self._d1_bias(d1)

        if h4_long and d1_long:
            bias = HTFBias.LONG_ONLY
        elif h4_short and d1_short:
            bias = HTFBias.SHORT_ONLY
        elif h4_long and not d1_short:
            bias = HTFBias.LEAN_LONG
        elif h4_short and not d1_long:
            bias = HTFBias.LEAN_SHORT
        else:
            bias = HTFBias.NEUTRAL

        logger.debug(
            "HTFBiasEngine: h4_long={} h4_short={} d1_long={} d1_short={} -> {}",
            h4_long, h4_short, d1_long, d1_short, bias.value,
        )
        return bias

    def _h4_bias(self, h4: pd.DataFrame) -> tuple[bool, bool]:
        if h4.empty or len(h4) < 220:
            return False, False
        ema50 = compute_ema(h4["close"], 50)
        ema200 = compute_ema(h4["close"], 200)
        if ema50.dropna().empty or ema200.dropna().empty:
            return False, False
        e50 = float(ema50.iloc[-1])
        e200 = float(ema200.iloc[-1])
        close = float(h4["close"].iloc[-1])
        long_ok = e50 > e200 and close > e50
        short_ok = e50 < e200 and close < e50
        return long_ok, short_ok

    def _d1_bias(self, d1: pd.DataFrame) -> tuple[bool, bool]:
        if d1.empty or len(d1) < 22:
            return False, False
        ema20 = compute_ema(d1["close"], 20)
        if ema20.dropna().empty:
            return False, False
        e20 = float(ema20.iloc[-1])
        close = float(d1["close"].iloc[-1])
        return close > e20, close < e20
