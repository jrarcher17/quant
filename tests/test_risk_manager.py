"""Tests for RiskManager — focused on the hedge-sizing path.

Avoids hitting the real DB by mocking out the trade-settings helper, the
feedback controller, and the daily-loss / concurrent-limit queries on the
RiskManager instance.
"""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.risk_manager import RiskManager
from app.services.trade_settings import TradeSettingsPayload
from app.strategies.base import CandidateSignal, Direction


def make_candidate(*, is_hedge: bool = False, **overrides) -> CandidateSignal:
    """Build a CandidateSignal with sane defaults; overridable per test."""
    defaults = dict(
        strategy_name="test_strategy",
        symbol="XAUUSD",
        timeframe="H1",
        direction=Direction.SELL,
        entry_price=Decimal("2700.00"),
        stop_loss=Decimal("2710.00"),
        take_profit_1=Decimal("2680.00"),
        take_profit_2=Decimal("2660.00"),
        risk_reward=Decimal("2.00"),
        confidence=Decimal("90.00"),
        reasoning="Test signal",
        timestamp=datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc),
        is_hedge=is_hedge,
    )
    defaults.update(overrides)
    return CandidateSignal(**defaults)


@pytest.fixture
def patched_settings():
    """Patch the config / trade-settings helpers used inside RiskManager.

    Returns a context manager factory:

        with patched_settings(hedge_risk_multiplier=0.5,
                              risk_per_trade_pct=0.01) as ctx:
            ...
    """
    from contextlib import contextmanager

    @contextmanager
    def _factory(
        risk_per_trade_pct: float = 0.01,
        hedge_risk_multiplier: float = 0.5,
        account_balance: float = 100_000.0,
    ):
        ts = TradeSettingsPayload(
            risk_per_trade_pct=risk_per_trade_pct,
            hedge_risk_multiplier=hedge_risk_multiplier,
        )
        mock_settings = MagicMock()
        mock_settings.account_balance = account_balance

        with (
            patch(
                "app.services.risk_manager.get_trade_settings",
                AsyncMock(return_value=ts),
            ),
            patch(
                "app.services.risk_manager.get_settings",
                MagicMock(return_value=mock_settings),
            ),
        ):
            yield ts

    return _factory


def stub_internal_checks(rm: RiskManager) -> None:
    """Bypass the daily-loss / concurrent-limit checks so the test focuses
    purely on the position-sizing path."""
    rm._check_daily_loss = AsyncMock(return_value=(False, 0.0))
    rm._check_concurrent_limit = AsyncMock(return_value=(False, 0))


def patch_circuit_breaker():
    """Patch FeedbackController (lazy-imported inside RiskManager.check)
    so the circuit breaker is always inactive."""
    fc_instance = MagicMock()
    fc_instance.check_circuit_breaker = AsyncMock(return_value=False)
    fc_cls = MagicMock(return_value=fc_instance)
    return patch(
        "app.services.feedback_controller.FeedbackController", fc_cls
    )


@pytest.mark.asyncio
async def test_primary_signal_uses_full_risk(patched_settings):
    """Non-hedge candidate gets the full risk_per_trade_pct budget."""
    rm = RiskManager()
    stub_internal_checks(rm)

    with patched_settings(risk_per_trade_pct=0.01, hedge_risk_multiplier=0.5):
        with patch_circuit_breaker():
            session = AsyncMock()

            results = await rm.check(
                session,
                [make_candidate(is_hedge=False)],
                current_atr=1.0,
                baseline_atr=1.0,
            )

    assert len(results) == 1
    _candidate, result = results[0]
    assert result.approved
    # Full 1% of $100k = $1000
    assert result.risk_amount == pytest.approx(1000.0)


@pytest.mark.asyncio
async def test_hedge_signal_halves_risk(patched_settings):
    """Hedge candidate gets risk_per_trade_pct * hedge_risk_multiplier."""
    rm = RiskManager()
    stub_internal_checks(rm)

    with patched_settings(risk_per_trade_pct=0.01, hedge_risk_multiplier=0.5):
        with patch_circuit_breaker():
            session = AsyncMock()

            results = await rm.check(
                session,
                [make_candidate(is_hedge=True)],
                current_atr=1.0,
                baseline_atr=1.0,
            )

    assert len(results) == 1
    _candidate, result = results[0]
    assert result.approved
    # 1% * 0.5 = 0.5% of $100k = $500
    assert result.risk_amount == pytest.approx(500.0)


@pytest.mark.asyncio
async def test_hedge_with_full_multiplier_matches_primary(patched_settings):
    """hedge_risk_multiplier=1.0 sizes hedges identically to primary signals."""
    rm = RiskManager()
    stub_internal_checks(rm)

    with patched_settings(risk_per_trade_pct=0.01, hedge_risk_multiplier=1.0):
        with patch_circuit_breaker():
            session = AsyncMock()

            results = await rm.check(
                session,
                [
                    make_candidate(is_hedge=False),
                    make_candidate(is_hedge=True),
                ],
                current_atr=1.0,
                baseline_atr=1.0,
            )

    primary = results[0][1]
    hedge = results[1][1]
    assert primary.approved and hedge.approved
    assert primary.risk_amount == pytest.approx(hedge.risk_amount)


@pytest.mark.asyncio
async def test_hedge_position_size_smaller_than_primary(patched_settings):
    """For identical SL distance and ATR, hedge position size < primary."""
    rm = RiskManager()
    stub_internal_checks(rm)

    with patched_settings(risk_per_trade_pct=0.01, hedge_risk_multiplier=0.5):
        with patch_circuit_breaker():
            session = AsyncMock()

            results = await rm.check(
                session,
                [
                    make_candidate(is_hedge=False),
                    make_candidate(is_hedge=True),
                ],
                current_atr=1.0,
                baseline_atr=1.0,
            )

    primary_size = float(results[0][1].position_size)
    hedge_size = float(results[1][1].position_size)
    assert hedge_size < primary_size
    # Hedge multiplier is 0.5, so size should be roughly half.
    assert hedge_size == pytest.approx(primary_size * 0.5)
