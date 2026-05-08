"""Unit tests for MacroBiasFilter.

Covers:
  - NEUTRAL / unavailable result when no data exists
  - DXY bearish signal (rising dollar → bearish gold)
  - DXY bullish signal (falling dollar → bullish gold)
  - VIX bullish signal (elevated fear → safe-haven demand)
  - VIX panic signal (spike amplification)
  - Combined BULLISH bias (DXY falling + elevated VIX)
  - apply() boosts aligned signals
  - apply() penalises opposed signals
  - apply() leaves signals untouched for NEUTRAL bias
  - apply() leaves signals untouched when bias is unavailable
  - compute_bias() gracefully degrades on unexpected exception
"""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.macro_filter import (
    BIAS_BEARISH_THRESHOLD,
    BIAS_BULLISH_THRESHOLD,
    DXY_MIN_CANDLES,
    MACRO_ALIGNED_BOOST,
    MACRO_OPPOSED_PENALTY,
    VIX_MIN_CANDLES,
    MacroBias,
    MacroBiasDirection,
    MacroBiasFilter,
)
from app.strategies.base import CandidateSignal, Direction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(dxy_closes: list[float], vix_closes: list[float]) -> AsyncMock:
    """Build an AsyncSession mock that returns DXY and VIX data in sequence.

    The first ``execute`` call returns DXY rows, the second returns VIX rows.
    Rows are returned in DESC order (newest first) as the real query does.
    """
    dxy_rows = [(Decimal(str(c)),) for c in reversed(dxy_closes)]
    vix_rows = [(Decimal(str(c)),) for c in reversed(vix_closes)]

    results = []
    for rows in [dxy_rows, vix_rows]:
        mock_result = MagicMock()
        mock_result.all.return_value = rows
        results.append(mock_result)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=results)
    return session


def _make_candidate(**overrides) -> CandidateSignal:
    defaults = dict(
        strategy_name="liquidity_sweep",
        symbol="XAUUSD",
        timeframe="H1",
        direction=Direction.BUY,
        entry_price=Decimal("2650.00"),
        stop_loss=Decimal("2645.00"),
        take_profit_1=Decimal("2660.00"),
        take_profit_2=Decimal("2670.00"),
        risk_reward=Decimal("3.00"),
        confidence=Decimal("70.00"),
        reasoning="Test signal",
        timestamp=datetime(2026, 2, 17, 12, 0, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return CandidateSignal(**defaults)


# ---------------------------------------------------------------------------
# compute_bias: no data → unavailable NEUTRAL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_bias_no_data_returns_unavailable():
    """Both DXY and VIX have no candles → unavailable NEUTRAL bias."""
    session = _make_session(dxy_closes=[], vix_closes=[])
    f = MacroBiasFilter()
    bias = await f.compute_bias(session)

    assert bias.available is False
    assert bias.direction == MacroBiasDirection.NEUTRAL
    assert bias.strength == 0.0


@pytest.mark.asyncio
async def test_compute_bias_insufficient_dxy_only_vix():
    """DXY below min_candles threshold → only VIX contributes."""
    # Fewer than DXY_MIN_CANDLES entries
    dxy = [100.0] * (DXY_MIN_CANDLES - 1)
    # VIX above panic level → large positive score → BULLISH
    vix = [35.0] * VIX_MIN_CANDLES
    session = _make_session(dxy, vix)
    f = MacroBiasFilter()
    bias = await f.compute_bias(session)

    assert bias.available is True
    assert bias.direction == MacroBiasDirection.BULLISH


# ---------------------------------------------------------------------------
# compute_bias: DXY signals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_bias_rising_dxy_bearish_gold():
    """Strong rising DXY (>1%) with no VIX data → BEARISH gold bias."""
    # Build 25 candles rising from 100 to ~102 (>1% gain)
    dxy = [100.0 + i * 0.1 for i in range(25)]
    # No VIX data
    vix: list[float] = []
    session = _make_session(dxy, vix)
    f = MacroBiasFilter()
    bias = await f.compute_bias(session)

    assert bias.available is True
    assert bias.direction == MacroBiasDirection.BEARISH
    assert any("bearish gold" in s for s in bias.signals)


@pytest.mark.asyncio
async def test_compute_bias_falling_dxy_bullish_gold():
    """Strong falling DXY (>1%) with no VIX data → BULLISH gold bias.

    Step of 0.15 gives a 10-day drop of ~-1.5%, triggering the strong score
    (30.0) which clears the BULLISH threshold (20.0) on its own.
    """
    dxy = [102.0 - i * 0.15 for i in range(25)]
    vix: list[float] = []
    session = _make_session(dxy, vix)
    f = MacroBiasFilter()
    bias = await f.compute_bias(session)

    assert bias.available is True
    assert bias.direction == MacroBiasDirection.BULLISH
    assert any("bullish gold" in s for s in bias.signals)


@pytest.mark.asyncio
async def test_compute_bias_flat_dxy_neutral():
    """Flat DXY (<0.5% change) with no VIX → NEUTRAL bias."""
    dxy = [100.0 + i * 0.01 for i in range(25)]  # only ~0.24% move
    vix: list[float] = []
    session = _make_session(dxy, vix)
    f = MacroBiasFilter()
    bias = await f.compute_bias(session)

    # Score is 0 (DXY neutral) and no VIX data → might still be available
    # with a neutral direction
    if bias.available:
        assert bias.direction == MacroBiasDirection.NEUTRAL


# ---------------------------------------------------------------------------
# compute_bias: VIX signals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_bias_panic_vix_bullish():
    """VIX > 30 panic level with flat DXY → BULLISH gold bias."""
    dxy = [100.0] * 25  # flat
    vix = [32.0] * 5
    session = _make_session(dxy, vix)
    f = MacroBiasFilter()
    bias = await f.compute_bias(session)

    assert bias.available is True
    assert bias.direction == MacroBiasDirection.BULLISH
    assert any("panic" in s.lower() or "risk-off" in s.lower() for s in bias.signals)


@pytest.mark.asyncio
async def test_compute_bias_vix_spike_amplifies():
    """VIX spike of >10 points amplifies the score further."""
    dxy = [100.0] * 25  # flat DXY
    # Window of 5: starts at 18, spikes to 30 (12pt spike + panic level)
    vix = [18.0, 19.0, 21.0, 26.0, 30.0]
    session = _make_session(dxy, vix)
    f = MacroBiasFilter()
    bias = await f.compute_bias(session)

    assert bias.available is True
    assert bias.direction == MacroBiasDirection.BULLISH
    # spike label should appear
    assert any("spike" in s.lower() for s in bias.signals)


@pytest.mark.asyncio
async def test_compute_bias_low_vix_does_not_push_bullish_alone():
    """VIX < 15 (low fear) with flat DXY should not reach BULLISH threshold."""
    dxy = [100.0] * 25
    vix = [12.0, 12.5, 13.0, 12.0, 11.5]
    session = _make_session(dxy, vix)
    f = MacroBiasFilter()
    bias = await f.compute_bias(session)

    if bias.available:
        assert bias.direction == MacroBiasDirection.NEUTRAL


# ---------------------------------------------------------------------------
# compute_bias: combined
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_bias_combined_bullish():
    """Falling DXY (moderate, +15) + elevated VIX 20-30 (+15) = score 30 → BULLISH.

    Step of 0.065 gives a 10-day DXY drop of ~-0.64%, hitting the moderate
    tier (+15). Combined with VIX elevated fear (+15), total = 30 >= threshold.
    """
    dxy = [102.0 - i * 0.065 for i in range(25)]
    vix = [22.0, 22.5, 23.0, 22.0, 22.5]
    session = _make_session(dxy, vix)
    f = MacroBiasFilter()
    bias = await f.compute_bias(session)

    assert bias.available is True
    assert bias.direction == MacroBiasDirection.BULLISH
    assert len(bias.signals) == 2


# ---------------------------------------------------------------------------
# apply()
# ---------------------------------------------------------------------------


def test_apply_boosts_aligned_buy_signal():
    """BUY signal + BULLISH macro → +MACRO_ALIGNED_BOOST confidence."""
    f = MacroBiasFilter()
    bias = MacroBias(
        direction=MacroBiasDirection.BULLISH,
        strength=30.0,
        signals=["DXY falling"],
        available=True,
        message="bullish",
    )
    candidate = _make_candidate(direction=Direction.BUY, confidence=Decimal("70.00"))
    result = f.apply([candidate], bias)

    assert len(result) == 1
    expected = round(70.0 + MACRO_ALIGNED_BOOST, 2)
    assert float(result[0].confidence) == pytest.approx(expected)
    assert "aligned" in result[0].reasoning


def test_apply_penalises_opposed_buy_signal():
    """BUY signal + BEARISH macro → -MACRO_OPPOSED_PENALTY confidence."""
    f = MacroBiasFilter()
    bias = MacroBias(
        direction=MacroBiasDirection.BEARISH,
        strength=25.0,
        signals=["DXY rising"],
        available=True,
        message="bearish",
    )
    candidate = _make_candidate(direction=Direction.BUY, confidence=Decimal("70.00"))
    result = f.apply([candidate], bias)

    expected = round(70.0 - MACRO_OPPOSED_PENALTY, 2)
    assert float(result[0].confidence) == pytest.approx(expected)
    assert "opposes" in result[0].reasoning


def test_apply_boosts_aligned_sell_signal():
    """SELL signal + BEARISH macro → +MACRO_ALIGNED_BOOST confidence."""
    f = MacroBiasFilter()
    bias = MacroBias(
        direction=MacroBiasDirection.BEARISH,
        strength=25.0,
        signals=["DXY rising"],
        available=True,
        message="bearish",
    )
    candidate = _make_candidate(direction=Direction.SELL, confidence=Decimal("65.00"))
    result = f.apply([candidate], bias)

    expected = round(65.0 + MACRO_ALIGNED_BOOST, 2)
    assert float(result[0].confidence) == pytest.approx(expected)
    assert "aligned" in result[0].reasoning


def test_apply_clamps_confidence_to_100():
    """Confidence cannot exceed 100 after boost."""
    f = MacroBiasFilter()
    bias = MacroBias(
        direction=MacroBiasDirection.BULLISH,
        strength=30.0,
        signals=[],
        available=True,
        message="bullish",
    )
    candidate = _make_candidate(direction=Direction.BUY, confidence=Decimal("98.00"))
    result = f.apply([candidate], bias)

    assert float(result[0].confidence) == pytest.approx(100.0)


def test_apply_clamps_confidence_to_zero():
    """Confidence cannot go below 0 after penalty."""
    f = MacroBiasFilter()
    bias = MacroBias(
        direction=MacroBiasDirection.BEARISH,
        strength=20.0,
        signals=[],
        available=True,
        message="bearish",
    )
    candidate = _make_candidate(direction=Direction.BUY, confidence=Decimal("10.00"))
    result = f.apply([candidate], bias)

    assert float(result[0].confidence) == pytest.approx(0.0)


def test_apply_neutral_bias_no_change():
    """NEUTRAL bias → candidates returned unchanged."""
    f = MacroBiasFilter()
    bias = MacroBias(
        direction=MacroBiasDirection.NEUTRAL,
        strength=0.0,
        signals=[],
        available=True,
        message="neutral",
    )
    candidate = _make_candidate(confidence=Decimal("70.00"))
    result = f.apply([candidate], bias)

    assert result is not None  # returned unchanged list
    # Pydantic models are value-equal when fields match
    assert float(result[0].confidence) == pytest.approx(70.0)
    assert result[0].reasoning == candidate.reasoning


def test_apply_unavailable_bias_no_change():
    """Unavailable bias → candidates returned unchanged."""
    f = MacroBiasFilter()
    bias = MacroBias(
        direction=MacroBiasDirection.NEUTRAL,
        strength=0.0,
        signals=[],
        available=False,
        message="unavailable",
    )
    candidate = _make_candidate(confidence=Decimal("70.00"))
    result = f.apply([candidate], bias)

    assert float(result[0].confidence) == pytest.approx(70.0)
    assert result[0].reasoning == candidate.reasoning


def test_apply_empty_list_returns_empty():
    """Empty candidates list → empty list returned."""
    f = MacroBiasFilter()
    bias = MacroBias(
        direction=MacroBiasDirection.BULLISH,
        strength=30.0,
        signals=[],
        available=True,
        message="bullish",
    )
    assert f.apply([], bias) == []


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_bias_degrades_on_exception():
    """If an unexpected exception occurs, compute_bias returns unavailable NEUTRAL."""
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=RuntimeError("DB exploded"))
    f = MacroBiasFilter()
    bias = await f.compute_bias(session)

    assert bias.available is False
    assert bias.direction == MacroBiasDirection.NEUTRAL
