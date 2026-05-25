"""Thin wrappers around pandas-ta for common technical indicators."""

import pandas as pd
import pandas_ta_classic as ta
from loguru import logger


def compute_ema(series: pd.Series, length: int) -> pd.Series:
    """Compute Exponential Moving Average.

    Args:
        series: Price series (typically close prices).
        length: EMA period length.

    Returns:
        EMA series of the same length as input (leading NaNs for warmup).
    """
    return ta.ema(series, length=length)


def compute_atr(
    high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14
) -> pd.Series:
    """Compute Average True Range.

    Args:
        high: High price series.
        low: Low price series.
        close: Close price series.
        length: ATR period length (default 14).

    Returns:
        ATR series of the same length as input (leading NaNs for warmup).
    """
    return ta.atr(high=high, low=low, close=close, length=length)


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """Compute Volume-Weighted Average Price.

    Requires a DataFrame with columns: timestamp, high, low, close, volume.
    Sets DatetimeIndex internally as required by pandas-ta VWAP.

    Args:
        df: DataFrame with OHLCV data and a 'timestamp' column.

    Returns:
        VWAP series aligned to the original DataFrame index.
        If volume is all zero/NaN, returns a Series of NaN.
    """
    df_copy = df.copy()

    # Check for valid volume data
    if "volume" not in df_copy.columns or df_copy["volume"].fillna(0).eq(0).all():
        logger.warning("VWAP: volume data is all zero or NaN; returning NaN series")
        return pd.Series([float("nan")] * len(df_copy), index=df_copy.index)

    # pandas-ta VWAP requires DatetimeIndex
    df_copy.index = pd.DatetimeIndex(df_copy["timestamp"])
    result = ta.vwap(
        high=df_copy["high"],
        low=df_copy["low"],
        close=df_copy["close"],
        volume=df_copy["volume"],
    )

    # Realign to original index
    if result is not None:
        result.index = df.index
        return result

    logger.warning("VWAP: pandas-ta returned None; returning NaN series")
    return pd.Series([float("nan")] * len(df), index=df.index)


def compute_rsi(series: pd.Series, length: int = 14) -> pd.Series:
    """Compute Relative Strength Index.

    Args:
        series: Price series (typically close prices).
        length: RSI period length (default 14).

    Returns:
        RSI series of the same length as input (leading NaNs for warmup).
    """
    return ta.rsi(series, length=length)


def compute_adx(
    high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14
) -> pd.Series:
    """Compute Average Directional Index (ADX) — trend strength 0-100.

    Returns the ADX line only. >25 typically indicates a trending market;
    <20 indicates a ranging market.
    """
    result = ta.adx(high=high, low=low, close=close, length=length)
    if result is None:
        return pd.Series([float("nan")] * len(close), index=close.index)
    col = f"ADX_{length}"
    if col in result.columns:
        return result[col]
    # pandas-ta-classic occasionally renames columns
    for c in result.columns:
        if c.startswith("ADX"):
            return result[c]
    return pd.Series([float("nan")] * len(close), index=close.index)


def atr_percentile_rank(atr_series: pd.Series, lookback: int = 200) -> float:
    """Return where the latest ATR sits within the rolling lookback window.

    Returns a float in [0.0, 1.0]. 0.5 means the latest ATR is exactly the
    median of the lookback window; 0.9 means it's higher than 90% of recent
    ATRs.
    """
    valid = atr_series.dropna()
    if len(valid) < 10:
        return 0.5
    window = valid.tail(lookback)
    latest = float(window.iloc[-1])
    rank = (window < latest).sum() / len(window)
    return float(rank)


def candle_body_overlap_ratio(c1_high: float, c1_low: float, c2_high: float, c2_low: float) -> float:
    """Compute how much two candles' ranges overlap on a 0..1 scale.

    1.0 means they fully overlap (chop); 0.0 means no overlap (clean break).
    """
    overlap = max(0.0, min(c1_high, c2_high) - max(c1_low, c2_low))
    union = max(c1_high, c2_high) - min(c1_low, c2_low)
    if union <= 0:
        return 0.0
    return float(overlap / union)


def avg_overlap_last_n(highs: pd.Series, lows: pd.Series, n: int = 10) -> float:
    """Average body overlap ratio over the last n candle pairs."""
    if len(highs) < n + 1:
        return 0.0
    h = highs.iloc[-(n + 1):].to_list()
    l = lows.iloc[-(n + 1):].to_list()
    pairs = [
        candle_body_overlap_ratio(h[i], l[i], h[i - 1], l[i - 1])
        for i in range(1, len(h))
    ]
    return float(sum(pairs) / len(pairs)) if pairs else 0.0
