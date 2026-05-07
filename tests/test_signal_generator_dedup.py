"""Tests for SignalGenerator's dedup logic.

Focuses on the `_is_duplicate` method, which now performs both a
time-window check and a price-distance check against active
same-direction signals.
"""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.signal_generator import SignalGenerator
from app.services.trade_settings import TradeSettingsPayload
from app.strategies.base import CandidateSignal, Direction


def make_candidate(**overrides) -> CandidateSignal:
    defaults = dict(
        strategy_name="ema_momentum",
        symbol="XAUUSD",
        timeframe="H1",
        direction=Direction.BUY,
        entry_price=Decimal("4704.08"),
        stop_loss=Decimal("4624.08"),
        take_profit_1=Decimal("4824.08"),
        take_profit_2=Decimal("4944.08"),
        risk_reward=Decimal("1.50"),
        confidence=Decimal("85.00"),
        reasoning="Test signal",
        timestamp=datetime(2026, 5, 6, 8, 0, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return CandidateSignal(**defaults)


def make_settings(**overrides) -> TradeSettingsPayload:
    return TradeSettingsPayload(**overrides)


class _StubResult:
    """Minimal stand-in for a SQLAlchemy Result -- only exposes the helpers
    `_is_duplicate` actually calls (`scalar_one_or_none`)."""

    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


def make_session(*, time_window_hit=None, price_distance_hit=None):
    """Build an AsyncMock session whose `execute` returns the given results
    in order. The first call corresponds to the time-window query, the
    second to the price-distance query."""
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _StubResult(time_window_hit),
            _StubResult(price_distance_hit),
        ]
    )
    return session


@pytest.mark.asyncio
async def test_no_duplicate_returns_none():
    """No active signal in either dimension -> not a duplicate."""
    sg = SignalGenerator()
    session = make_session(time_window_hit=None, price_distance_hit=None)
    candidate = make_candidate()

    with patch(
        "app.services.signal_generator.get_trade_settings",
        AsyncMock(return_value=make_settings(dedup_price_distance_pips=30.0)),
    ):
        reason = await sg._is_duplicate(session, candidate)

    assert reason is None


@pytest.mark.asyncio
async def test_duplicate_within_time_window():
    """An active same-direction signal in the last hour is a duplicate."""
    sg = SignalGenerator()
    # The time-window query returns a Signal.id; the price-distance query
    # is never reached because the time-window check short-circuits.
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_StubResult(42)])
    candidate = make_candidate()

    with patch(
        "app.services.signal_generator.get_trade_settings",
        AsyncMock(return_value=make_settings(dedup_price_distance_pips=30.0)),
    ):
        reason = await sg._is_duplicate(session, candidate)

    assert reason is not None
    assert "1h window" in reason
    # Only the first query should run.
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_duplicate_at_near_identical_entry_price():
    """No time-window hit, but an active same-direction signal exists at a
    near-identical entry price -> blocked by the new price-distance check."""
    sg = SignalGenerator()
    # First query (time-window) misses, second query (price-distance) hits.
    session = make_session(
        time_window_hit=None,
        price_distance_hit=Decimal("4704.08"),
    )
    candidate = make_candidate(entry_price=Decimal("4704.08"))

    with patch(
        "app.services.signal_generator.get_trade_settings",
        AsyncMock(return_value=make_settings(dedup_price_distance_pips=30.0)),
    ):
        reason = await sg._is_duplicate(session, candidate)

    assert reason is not None
    assert "active same-direction" in reason
    assert "4704.08" in reason


@pytest.mark.asyncio
async def test_distance_disabled_falls_back_to_time_window_only():
    """Setting dedup_price_distance_pips=0 disables the new check."""
    sg = SignalGenerator()
    # Time-window misses; price-distance query should NOT run because
    # the distance check is disabled.
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_StubResult(None)])
    candidate = make_candidate()

    with patch(
        "app.services.signal_generator.get_trade_settings",
        AsyncMock(return_value=make_settings(dedup_price_distance_pips=0.0)),
    ):
        reason = await sg._is_duplicate(session, candidate)

    assert reason is None
    # Only the time-window query ran; the second query is skipped.
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_far_away_active_signal_passes():
    """Active same-direction signal exists, but far from candidate -- the
    SQL filter is responsible for excluding it; we simulate that by having
    the price-distance query return None."""
    sg = SignalGenerator()
    session = make_session(time_window_hit=None, price_distance_hit=None)
    candidate = make_candidate(entry_price=Decimal("5000.00"))

    with patch(
        "app.services.signal_generator.get_trade_settings",
        AsyncMock(return_value=make_settings(dedup_price_distance_pips=30.0)),
    ):
        reason = await sg._is_duplicate(session, candidate)

    assert reason is None
