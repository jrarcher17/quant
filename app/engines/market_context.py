"""MarketContext — single object containing all engine outputs for one tick.

Built once per scanner cycle. Strategies, the scoring engine, and the risk
modules all consume it instead of re-querying the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import pandas as pd
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.candle import Candle
from app.strategies.base import candles_to_dataframe

if TYPE_CHECKING:
    from app.engines.htf_bias_engine import HTFBias
    from app.engines.liquidity_engine import LiquidityMap
    from app.engines.news_engine import NewsWindow
    from app.engines.regime_engine import Regime
    from app.engines.session_engine import SessionInfo


@dataclass
class MarketContext:
    """Snapshot of market state used to evaluate signals."""

    timestamp: datetime
    symbol: str

    h1: pd.DataFrame
    h4: pd.DataFrame
    d1: pd.DataFrame

    regime: "Regime"
    htf_bias: "HTFBias"
    liquidity: "LiquidityMap"
    session: "SessionInfo"
    news: "NewsWindow"

    # Convenience scalars (populated by build_market_context)
    atr: float = 0.0
    atr_avg: float = 0.0
    atr_percentile: float = 0.5
    adx: float = 0.0
    last_bar_range: float = 0.0
    rsi: float = 50.0

    extras: dict = field(default_factory=dict)

    def as_snapshot(self) -> dict:
        """Return a JSON-safe summary used in DecisionLogger."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "regime": self.regime.value,
            "htf_bias": self.htf_bias.value,
            "session": self.session.label,
            "session_quality": self.session.quality_multiplier,
            "news_blocked": self.news.blocked,
            "atr": round(self.atr, 4),
            "atr_avg": round(self.atr_avg, 4),
            "atr_percentile": round(self.atr_percentile, 3),
            "adx": round(self.adx, 2),
            "last_bar_range": round(self.last_bar_range, 4),
            "rsi": round(self.rsi, 2),
        }


async def _load_candles(
    session: AsyncSession, symbol: str, timeframe: str, limit: int
) -> pd.DataFrame:
    stmt = (
        select(Candle)
        .where(Candle.symbol == symbol, Candle.timeframe == timeframe)
        .order_by(Candle.timestamp.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    if not rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    return candles_to_dataframe(rows)


# Dashboard polls /dashboard/regime every 30s — cache avoids reloading
# 800+ candles and recomputing engines on every request.
_CONTEXT_CACHE: tuple[datetime, MarketContext] | None = None
_CONTEXT_CACHE_TTL_SEC = 60


async def build_market_context(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    use_cache: bool = True,
) -> MarketContext:
    """Construct a fresh MarketContext from the database.

    Loads H1/H4/D1 candles, runs all engines, and returns a single immutable
    object describing the current market state.
    """
    global _CONTEXT_CACHE

    ts = now or datetime.now(tz=__import__("datetime").timezone.utc)
    if use_cache and _CONTEXT_CACHE is not None:
        cached_at, cached_ctx = _CONTEXT_CACHE
        age = (ts - cached_at).total_seconds()
        if age < _CONTEXT_CACHE_TTL_SEC:
            return cached_ctx

    from app.engines.regime_engine import RegimeEngine
    from app.engines.htf_bias_engine import HTFBiasEngine
    from app.engines.liquidity_engine import LiquidityEngine
    from app.engines.session_engine import SessionEngine
    from app.engines.news_engine import NewsEngine
    from app.strategies.helpers.indicators import (
        atr_percentile_rank,
        compute_adx,
        compute_atr,
        compute_rsi,
    )

    settings = get_settings()
    symbol = settings.trading_symbol

    h1 = await _load_candles(session, symbol, "H1", 400)
    h4 = await _load_candles(session, symbol, "H4", 200)
    d1 = await _load_candles(session, symbol, "D1", 200)

    regime_engine = RegimeEngine()
    htf_engine = HTFBiasEngine()
    liq_engine = LiquidityEngine()
    sess_engine = SessionEngine()
    news_engine = NewsEngine()

    regime = regime_engine.classify(h1, h4)
    htf_bias = htf_engine.bias(h4, d1)
    liquidity = liq_engine.build_map(h1, d1)
    session_info = sess_engine.classify(ts)
    news_window = await news_engine.window(session, ts)

    atr_val = 0.0
    atr_avg = 0.0
    atr_pct = 0.5
    adx_val = 0.0
    last_range = 0.0
    rsi_val = 50.0

    if not h1.empty and len(h1) >= 30:
        atr_series = compute_atr(h1["high"], h1["low"], h1["close"], length=14)
        atr_valid = atr_series.dropna()
        if not atr_valid.empty:
            atr_val = float(atr_valid.iloc[-1])
            atr_avg = float(atr_valid.tail(50).mean())
            atr_pct = atr_percentile_rank(atr_series, lookback=200)
        adx_series = compute_adx(h1["high"], h1["low"], h1["close"], length=14)
        adx_valid = adx_series.dropna()
        if not adx_valid.empty:
            adx_val = float(adx_valid.iloc[-1])
        rsi_series = compute_rsi(h1["close"], length=14)
        rsi_valid = rsi_series.dropna()
        if not rsi_valid.empty:
            rsi_val = float(rsi_valid.iloc[-1])
        last_range = float(h1["high"].iloc[-1] - h1["low"].iloc[-1])

    ctx = MarketContext(
        timestamp=ts,
        symbol=symbol,
        h1=h1,
        h4=h4,
        d1=d1,
        regime=regime,
        htf_bias=htf_bias,
        liquidity=liquidity,
        session=session_info,
        news=news_window,
        atr=atr_val,
        atr_avg=atr_avg,
        atr_percentile=atr_pct,
        adx=adx_val,
        last_bar_range=last_range,
        rsi=rsi_val,
    )

    logger.info(
        "MarketContext: regime={} htf={} session={}({:.2f}) news_block={} adx={:.1f} atr_pct={:.2f}",
        regime.value,
        htf_bias.value,
        session_info.label,
        session_info.quality_multiplier,
        news_window.blocked,
        adx_val,
        atr_pct,
    )

    if use_cache:
        _CONTEXT_CACHE = (ts, ctx)

    return ctx
