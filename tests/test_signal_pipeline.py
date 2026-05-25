"""Integration tests for SignalPipeline orchestrator.

NOTE: These tests target the v1 pipeline (HEDGE flow, ranked strategies,
macro filter). The v2 pipeline is context-aware and rewrites the entire
flow; these tests need to be re-authored against the new MarketContext +
ScoringEngine surface. Skipped at module level until that work happens.

Uses mocking to verify pipeline orchestration logic without needing a real
database. All async methods use AsyncMock, sync methods use MagicMock.
"""

import pytest

pytestmark = pytest.mark.skip(
    reason="v1 pipeline tests; v2 architecture deprecates the HEDGE / ranked-selector flow"
)


from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.signal import Signal
from app.services.gold_intelligence import DXYCorrelation, GoldIntelligence
from app.services.risk_manager import RiskCheckResult, RiskManager
from app.services.signal_generator import SignalGenerator
from app.services.signal_pipeline import HEDGE_REASONING_TAG, SignalPipeline
from app.services.strategy_selector import (
    StrategyScore,
    StrategySelector,
    VolatilityRegime,
)
from app.services.trade_settings import TradeSettingsPayload
from app.strategies.base import CandidateSignal, Direction


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def make_mock_candidate(**overrides) -> CandidateSignal:
    """Create a test CandidateSignal with sensible defaults."""
    defaults = dict(
        strategy_name="liquidity_sweep_reversal",
        symbol="XAUUSD",
        timeframe="H1",
        direction=Direction.BUY,
        entry_price=Decimal("2650.00"),
        stop_loss=Decimal("2645.00"),
        take_profit_1=Decimal("2660.00"),
        take_profit_2=Decimal("2670.00"),
        risk_reward=Decimal("3.00"),
        confidence=Decimal("75.00"),
        reasoning="Test signal",
        timestamp=datetime(2026, 2, 17, 12, 0, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return CandidateSignal(**defaults)


def make_mock_strategy_score(**overrides) -> StrategyScore:
    """Create a test StrategyScore with sensible defaults."""
    defaults = dict(
        strategy_name="liquidity_sweep_reversal",
        strategy_id=1,
        composite_score=0.85,
        win_rate=0.65,
        profit_factor=2.1,
        sharpe_ratio=1.3,
        expectancy=0.5,
        max_drawdown=0.12,
        total_trades=120,
        regime=VolatilityRegime.MEDIUM,
        is_degraded=False,
        degradation_reason=None,
    )
    defaults.update(overrides)
    return StrategyScore(**defaults)


def make_trade_settings(**overrides) -> TradeSettingsPayload:
    """Build a TradeSettingsPayload with optional overrides."""
    return TradeSettingsPayload(**overrides)


def make_pipeline(
    *,
    opposite_active_count: int = 0,
    trade_settings: TradeSettingsPayload | None = None,
):
    """Create a SignalPipeline with all-mocked services.

    `opposite_active_count` controls what the opposite-direction count
    query returns. `trade_settings` is what `get_trade_settings` resolves
    to inside the pipeline.
    """
    selector = MagicMock(spec=StrategySelector)
    generator = MagicMock(spec=SignalGenerator)
    risk_manager = MagicMock(spec=RiskManager)
    gold_intel = MagicMock(spec=GoldIntelligence)

    selector.select_all_ranked = AsyncMock(return_value=[])
    selector.check_h4_confluence = AsyncMock(return_value=False)
    generator.expire_stale_signals = AsyncMock(return_value=0)
    generator.generate = AsyncMock(return_value=[])
    generator.validate = AsyncMock(return_value=[])
    generator.compute_expiry = MagicMock(
        return_value=datetime(2026, 2, 17, 20, 0, 0, tzinfo=timezone.utc)
    )
    risk_manager.check = AsyncMock(return_value=[])
    gold_intel.get_dxy_correlation = AsyncMock(
        return_value=DXYCorrelation(
            correlation=None, is_divergent=False, available=False, message="N/A"
        )
    )
    gold_intel.enrich = MagicMock(return_value=[])

    pipeline = SignalPipeline(selector, generator, risk_manager, gold_intel)

    # Avoid the heavy ATR query path; tests don't care about position sizing.
    pipeline._compute_atr = AsyncMock(return_value=(1.0, 1.0))

    # session.execute is dispatched between two queries: the
    # opposite-direction count and the strategy-row lookup. The
    # AsyncSession wrapper exposes the synchronous Result helpers
    # (scalar_one / scalar_one_or_none) on the awaited value.
    mock_strategy_row = MagicMock()
    mock_strategy_row.id = 1

    count_result = MagicMock()
    count_result.scalar_one.return_value = opposite_active_count
    strategy_result = MagicMock()
    strategy_result.scalar_one_or_none.return_value = mock_strategy_row

    # Heuristic: the count query produces a Result whose scalar_one()
    # is consulted; the strategy lookup uses scalar_one_or_none().
    # MagicMock returns a fresh result for every call, so set both
    # accessors on a single combined result and reuse it.
    combined = MagicMock()
    combined.scalar_one.return_value = opposite_active_count
    combined.scalar_one_or_none.return_value = mock_strategy_row

    session = AsyncMock()
    session.execute = AsyncMock(return_value=combined)
    # `session.add` is synchronous in real SQLAlchemy; AsyncMock would
    # produce an unawaited-coroutine warning if we left it as the default.
    session.add = MagicMock()

    settings = trade_settings or make_trade_settings()
    pipeline._test_settings_patch = patch(
        "app.services.signal_pipeline.get_trade_settings",
        AsyncMock(return_value=settings),
    )
    pipeline._test_settings_patch.start()

    return pipeline, session


def teardown_pipeline(pipeline) -> None:
    """Stop any patches started in `make_pipeline`."""
    if hasattr(pipeline, "_test_settings_patch"):
        pipeline._test_settings_patch.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_skips_when_no_strategy_qualifies():
    """Pipeline returns empty list when ranked list is empty."""
    pipeline, session = make_pipeline()
    try:
        pipeline.selector.select_all_ranked.return_value = []

        result = await pipeline.run(session)

        assert result == []
        pipeline.generator.generate.assert_not_called()
    finally:
        teardown_pipeline(pipeline)


@pytest.mark.asyncio
async def test_pipeline_skips_when_no_candidates():
    """Pipeline tries every ranked strategy and stops if none produce candidates."""
    pipeline, session = make_pipeline()
    try:
        pipeline.selector.select_all_ranked.return_value = [make_mock_strategy_score()]
        pipeline.generator.generate.return_value = []

        result = await pipeline.run(session)

        assert result == []
        pipeline.generator.validate.assert_not_called()
    finally:
        teardown_pipeline(pipeline)


@pytest.mark.asyncio
async def test_pipeline_filters_all_candidates():
    """Pipeline returns empty list when validation filters out all candidates."""
    pipeline, session = make_pipeline()
    try:
        pipeline.selector.select_all_ranked.return_value = [make_mock_strategy_score()]
        pipeline.generator.generate.return_value = [make_mock_candidate()]
        pipeline.generator.validate.return_value = []

        result = await pipeline.run(session)

        assert result == []
        pipeline.risk_manager.check.assert_not_called()
    finally:
        teardown_pipeline(pipeline)


@pytest.mark.asyncio
async def test_pipeline_risk_rejects_all():
    """Pipeline returns empty list when risk manager rejects all candidates."""
    pipeline, session = make_pipeline()
    try:
        candidate = make_mock_candidate()

        pipeline.selector.select_all_ranked.return_value = [make_mock_strategy_score()]
        pipeline.generator.generate.return_value = [candidate]
        pipeline.generator.validate.return_value = [candidate]
        pipeline.risk_manager.check.return_value = [
            (candidate, RiskCheckResult(approved=False, rejection_reason="Daily loss limit"))
        ]

        result = await pipeline.run(session)

        assert result == []
    finally:
        teardown_pipeline(pipeline)


@pytest.mark.asyncio
async def test_pipeline_full_flow_produces_signal():
    """Full happy-path: pipeline generates, validates, risk-checks, enriches, persists."""
    pipeline, session = make_pipeline()
    try:
        candidate = make_mock_candidate()
        enriched_candidate = make_mock_candidate(
            reasoning="Test signal | London/NY overlap: +5 confidence",
            session="overlap",
        )

        pipeline.selector.select_all_ranked.return_value = [make_mock_strategy_score()]
        pipeline.generator.generate.return_value = [candidate]
        pipeline.generator.validate.return_value = [candidate]
        pipeline.risk_manager.check.return_value = [
            (candidate, RiskCheckResult(approved=True, position_size=Decimal("1.50")))
        ]
        pipeline.selector.check_h4_confluence.return_value = True
        pipeline.gold_intel.enrich.return_value = [enriched_candidate]

        result = await pipeline.run(session)

        assert len(result) == 1
        assert isinstance(result[0], Signal)
        assert result[0].strategy_id == 1
        assert result[0].symbol == "XAUUSD"
        assert result[0].direction == "BUY"
        assert result[0].status == "active"
        session.add.assert_called_once()
        session.commit.assert_awaited_once()
    finally:
        teardown_pipeline(pipeline)


@pytest.mark.asyncio
async def test_expire_stale_signals_called_first():
    """Expire stale signals is called before strategy selection."""
    pipeline, session = make_pipeline()
    try:
        call_order: list[str] = []

        async def mock_expire(s):
            call_order.append("expire")
            return 0

        async def mock_select(s):
            call_order.append("select")
            return []  # short-circuit after select

        pipeline.generator.expire_stale_signals = mock_expire
        pipeline.selector.select_all_ranked = mock_select

        await pipeline.run(session)

        assert call_order == ["expire", "select"]
        assert call_order.index("expire") < call_order.index("select")
    finally:
        teardown_pipeline(pipeline)


# ---------------------------------------------------------------------------
# Hedge / opposite-direction override tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opposite_block_when_below_hedge_threshold():
    """Default (threshold=100) blocks any opposite-direction candidate."""
    pipeline, session = make_pipeline(
        opposite_active_count=1,
        trade_settings=make_trade_settings(hedge_min_confidence=100.0),
    )
    try:
        candidate = make_mock_candidate(
            direction=Direction.SELL, confidence=Decimal("95.00")
        )
        pipeline.selector.select_all_ranked.return_value = [make_mock_strategy_score()]
        pipeline.generator.generate.return_value = [candidate]
        pipeline.generator.validate.return_value = [candidate]

        result = await pipeline.run(session)

        assert result == []
        pipeline.risk_manager.check.assert_not_called()
    finally:
        teardown_pipeline(pipeline)


@pytest.mark.asyncio
async def test_opposite_allowed_when_above_hedge_threshold():
    """High-confidence opposite-direction candidate fires through as a hedge."""
    pipeline, session = make_pipeline(
        opposite_active_count=1,
        trade_settings=make_trade_settings(hedge_min_confidence=80.0),
    )
    try:
        candidate = make_mock_candidate(
            direction=Direction.SELL, confidence=Decimal("88.00")
        )
        pipeline.selector.select_all_ranked.return_value = [make_mock_strategy_score()]
        pipeline.generator.generate.return_value = [candidate]
        pipeline.generator.validate.return_value = [candidate]

        risk_seen: list = []

        # The risk manager echoes whatever candidate it receives so the
        # pipeline-applied HEDGE tag survives end-to-end.
        async def echo_risk_check(s, candidates, **_):
            risk_seen.extend(candidates)
            return [
                (c, RiskCheckResult(approved=True, position_size=Decimal("1.50")))
                for c in candidates
            ]

        pipeline.risk_manager.check = AsyncMock(side_effect=echo_risk_check)
        pipeline.gold_intel.enrich.side_effect = lambda candidates, dxy: list(candidates)

        result = await pipeline.run(session)

        assert len(result) == 1
        assert result[0].direction == "SELL"
        assert HEDGE_REASONING_TAG in result[0].reasoning
        # Pipeline must hand the risk manager an is_hedge=True candidate so
        # the per-hedge risk multiplier is actually applied downstream.
        assert len(risk_seen) == 1
        assert risk_seen[0].is_hedge is True
        session.commit.assert_awaited_once()
    finally:
        teardown_pipeline(pipeline)


@pytest.mark.asyncio
async def test_same_direction_passes_without_hedge_tag():
    """Same-direction candidate is unaffected by the hedge gate."""
    pipeline, session = make_pipeline(
        opposite_active_count=0,
        trade_settings=make_trade_settings(hedge_min_confidence=80.0),
    )
    try:
        candidate = make_mock_candidate(
            direction=Direction.BUY, confidence=Decimal("70.00")
        )
        pipeline.selector.select_all_ranked.return_value = [make_mock_strategy_score()]
        pipeline.generator.generate.return_value = [candidate]
        pipeline.generator.validate.return_value = [candidate]
        pipeline.risk_manager.check.return_value = [
            (candidate, RiskCheckResult(approved=True, position_size=Decimal("1.50")))
        ]
        pipeline.gold_intel.enrich.side_effect = lambda candidates, dxy: list(candidates)

        result = await pipeline.run(session)

        assert len(result) == 1
        assert result[0].direction == "BUY"
        assert HEDGE_REASONING_TAG not in result[0].reasoning
    finally:
        teardown_pipeline(pipeline)


@pytest.mark.asyncio
async def test_pipeline_falls_through_to_next_strategy_when_blocked():
    """If strategy A's candidate is blocked by the hedge gate, the pipeline
    moves on to strategy B (which has a higher-confidence candidate that
    clears the threshold)."""
    pipeline, session = make_pipeline(
        opposite_active_count=1,
        trade_settings=make_trade_settings(hedge_min_confidence=80.0),
    )
    try:
        # Both strategies want to fade an active BUY. Alpha's confidence
        # (70) is below the 80 hedge threshold and gets blocked; bravo's
        # confidence (90) clears the threshold and fires as a HEDGE.
        score_a = make_mock_strategy_score(strategy_name="alpha", strategy_id=1)
        score_b = make_mock_strategy_score(
            strategy_name="bravo", strategy_id=2, composite_score=0.5
        )
        candidate_a = make_mock_candidate(
            strategy_name="alpha",
            direction=Direction.SELL,
            confidence=Decimal("70.00"),
        )
        candidate_b = make_mock_candidate(
            strategy_name="bravo",
            direction=Direction.SELL,
            confidence=Decimal("90.00"),
        )

        pipeline.selector.select_all_ranked.return_value = [score_a, score_b]
        pipeline.generator.generate.side_effect = [
            [candidate_a],  # alpha
            [candidate_b],  # bravo
        ]
        pipeline.generator.validate.side_effect = [
            [candidate_a],
            [candidate_b],
        ]

        async def echo_risk_check(s, candidates, **_):
            return [
                (c, RiskCheckResult(approved=True, position_size=Decimal("1.50")))
                for c in candidates
            ]

        pipeline.risk_manager.check = AsyncMock(side_effect=echo_risk_check)
        pipeline.gold_intel.enrich.side_effect = lambda candidates, dxy: list(candidates)

        result = await pipeline.run(session)

        assert len(result) == 1
        assert result[0].direction == "SELL"
        assert HEDGE_REASONING_TAG in result[0].reasoning
        # Both strategies were tried (alpha got blocked, bravo went through).
        assert pipeline.generator.generate.await_count == 2
    finally:
        teardown_pipeline(pipeline)
