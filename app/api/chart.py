"""Chart data API endpoints for dashboard candlestick visualization."""

import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_session
from app.models.candle import Candle
from app.models.outcome import Outcome
from app.models.signal import Signal
from app.services.trade_settings import get_trade_settings

router = APIRouter(prefix="/chart", tags=["chart"])

VALID_TIMEFRAMES = {"M15", "H1", "H4", "D1"}
MAX_RANGE_DAYS = 14


def _to_unix_seconds(dt: datetime.datetime) -> int:
    """Convert a datetime to Unix seconds (int), assuming UTC if naive."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp())


def _outcome_color(result: str | None, status: str) -> str:
    """Determine signal marker color based on outcome and status."""
    if result in ("tp1_hit", "tp2_hit"):
        return "#26a69a"  # green
    if result == "sl_hit":
        return "#ef5350"  # red
    if status == "active" and result is None:
        return "#3179F5"  # blue
    return "#888888"  # gray (expired or other)


def _is_valid_chart_candle(candle: Candle, median_close: float) -> bool:
    """Filter obvious bad ticks so one outlier does not flatten the chart."""
    values = [
        float(candle.open),
        float(candle.high),
        float(candle.low),
        float(candle.close),
    ]
    if any(value <= 0 for value in values):
        return False
    if not (values[2] <= min(values[0], values[3]) <= max(values[0], values[3]) <= values[1]):
        return False
    # XAUUSD should not move anywhere near this much inside one dashboard range.
    return all(median_close * 0.5 <= value <= median_close * 1.5 for value in values)


@router.get("/candles")
async def get_chart_candles(
    timeframe: str = Query(default="H1", pattern="^(M15|H1|H4|D1)$"),
    range_days: float = Query(default=14, gt=0, le=MAX_RANGE_DAYS),
    limit: int = Query(default=1500, gt=0, le=5000),
    session: AsyncSession = Depends(get_session),
):
    """Return candle data as JSON with Unix timestamp seconds.

    Candles are returned in chronological order (oldest first) as required
    by TradingView Lightweight Charts.
    """
    try:
        ts_settings = await get_trade_settings(session)
        symbol = ts_settings.trading_symbol or get_settings().trading_symbol
        timeframe = timeframe.upper()
        if timeframe not in VALID_TIMEFRAMES:
            timeframe = "H1"

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=range_days)
        query = (
            select(Candle)
            .where(Candle.symbol == symbol)
            .where(Candle.timeframe == timeframe)
            .where(Candle.timestamp >= cutoff)
            .order_by(Candle.timestamp.desc())
            .limit(limit)
        )
        result = await session.execute(query)
        candles = result.scalars().all()

        # Reverse to chronological order (oldest first)
        candles.reverse()
        if candles:
            closes = sorted(float(c.close) for c in candles if float(c.close) > 0)
            if closes:
                median_close = closes[len(closes) // 2]
                candles = [
                    candle for candle in candles
                    if _is_valid_chart_candle(candle, median_close)
                ]

        return [
            {
                "time": _to_unix_seconds(c.timestamp),
                "open": float(c.open),
                "high": float(c.high),
                "low": float(c.low),
                "close": float(c.close),
            }
            for c in candles
        ]
    except Exception:
        return []


@router.get("/signals")
async def get_chart_signals(
    range_days: float = Query(default=14, gt=0, le=MAX_RANGE_DAYS),
    limit: int = Query(default=100, gt=0, le=500),
    session: AsyncSession = Depends(get_session),
):
    """Return signal data with outcome colors for chart markers.

    Each signal includes entry/SL/TP prices, direction, outcome color,
    and timing information for marker placement.
    """
    try:
        ts_settings = await get_trade_settings(session)
        symbol = ts_settings.trading_symbol or get_settings().trading_symbol
        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=range_days)
        query = (
            select(Signal, Outcome)
            .outerjoin(Outcome, Signal.id == Outcome.signal_id)
            .where(Signal.symbol == symbol)
            .where(Signal.created_at >= cutoff)
            .order_by(Signal.created_at.desc())
            .limit(limit)
        )
        result = await session.execute(query)
        rows = result.all()

        signals = []
        for signal, outcome in rows:
            result_str = outcome.result if outcome else None
            signals.append(
                {
                    "time": _to_unix_seconds(signal.created_at),
                    "direction": signal.direction,
                    "entry_price": float(signal.entry_price),
                    "stop_loss": float(signal.stop_loss),
                    "take_profit_1": float(signal.take_profit_1),
                    "take_profit_2": float(signal.take_profit_2),
                    "status": signal.status,
                    "outcome_color": _outcome_color(result_str, signal.status),
                    "confidence": float(signal.confidence),
                }
            )

        return signals
    except Exception:
        return []
