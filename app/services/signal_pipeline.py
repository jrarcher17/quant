"""SignalPipeline v2 — context-aware orchestrator.

New flow per scanner tick:

    1. Build MarketContext (regime / HTF / liquidity / session / news)
    2. If session blocked or news blocked or regime CHOP → emit no signals
    3. RegimeStrategySelector.allowed(regime) → list of strategy names
    4. For each allowed strategy:
         a. generate candidates (existing SignalGenerator)
         b. run lightweight validation (R:R, dedup) — kept from old pipeline
         c. for each candidate:
              - apply structural stop (StopEngine)
              - apply dynamic target (TargetEngine)
              - update entry-relative SL/TP on the candidate
              - score with ScoringEngine
              - log decision (accept or reject) via DecisionLogger
         d. keep the highest-scoring candidate per strategy
    5. Across allowed strategies, persist at most ONE highest-scoring signal
       that beats the score threshold.
    6. Old enrichment (gold intel, H4 confluence boost, macro bias) still
       runs but is informational only — it shifts confidence, never blocks.

This pipeline is the public `SignalPipeline.run()` so existing callers
(jobs.py / `run_signal_scanner`) keep working without changes.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.engines.market_context import MarketContext, build_market_context
from app.engines.regime_engine import Regime
from app.engines.scoring_engine import (
    KIND_BREAKOUT,
    KIND_EMA_MOM,
    KIND_LIQ_SWEEP,
    KIND_TREND_CONT,
    ScoringEngine,
    SignalScore,
)
from app.engines.strategy_selector_v2 import RegimeStrategySelector
from app.models.signal import Signal
from app.models.strategy import Strategy as StrategyModel
from app.risk.stop_engine import StopEngine, StopPlan
from app.risk.target_engine import TargetEngine, TargetPlan
from app.services.decision_logger import DecisionLogger

if TYPE_CHECKING:
    from app.services.gold_intelligence import GoldIntelligence
    from app.services.macro_filter import MacroBiasFilter
    from app.services.risk_manager import RiskManager
    from app.services.signal_generator import SignalGenerator
    from app.services.strategy_selector import StrategySelector


# Backwards-compat token for any callers still importing it from the old pipeline.
HEDGE_REASONING_TAG = "[HEDGE: opposite-direction override]"

_KIND_BY_STRATEGY = {
    "liquidity_sweep": KIND_LIQ_SWEEP,
    "trend_continuation": KIND_TREND_CONT,
    "breakout_expansion": KIND_BREAKOUT,
    "ema_momentum": KIND_EMA_MOM,
}


class SignalPipeline:
    def __init__(
        self,
        selector: "StrategySelector",
        generator: "SignalGenerator",
        risk_manager: "RiskManager",
        gold_intel: "GoldIntelligence",
        macro_filter: "MacroBiasFilter | None" = None,
        score_threshold: float = 6.5,
    ) -> None:
        self.selector = selector
        self.generator = generator
        self.risk_manager = risk_manager
        self.gold_intel = gold_intel
        self.macro_filter = macro_filter

        self.regime_selector = RegimeStrategySelector()
        self.scoring = ScoringEngine()
        self.stop_engine = StopEngine()
        self.target_engine = TargetEngine()
        self.score_threshold = score_threshold

    # ------------------------------------------------------------------
    async def run(self, session: AsyncSession) -> list[Signal]:
        # 0. Expire stale signals (kept from old pipeline)
        try:
            await self.generator.expire_stale_signals(session)
        except Exception:
            logger.exception("Pipeline: expire_stale_signals failed (non-fatal)")

        # 1. Build context
        ctx = await build_market_context(session)

        # 2. Hard blocks — log a single decision so the dashboard explains why
        if ctx.session.blocked:
            await DecisionLogger.log(
                session=session, candidate=None, accepted=False,
                ctx=ctx, rejection_reason=f"session_blocked:{ctx.session.label}",
            )
            await session.commit()
            return []
        if ctx.news.blocked:
            await DecisionLogger.log(
                session=session, candidate=None, accepted=False,
                ctx=ctx, rejection_reason=f"news_blackout:{ctx.news.reason}",
            )
            await session.commit()
            return []
        if ctx.regime in (Regime.CHOP, Regime.UNKNOWN):
            await DecisionLogger.log(
                session=session, candidate=None, accepted=False,
                ctx=ctx, rejection_reason=f"regime:{ctx.regime.value}",
            )
            await session.commit()
            return []

        allowed = self.regime_selector.allowed(ctx.regime)
        if not allowed:
            return []

        # 3. Try each allowed strategy
        all_scored: list[tuple[float, object, str, StopPlan, TargetPlan, SignalScore]] = []

        for strategy_name in allowed:
            kind = _KIND_BY_STRATEGY.get(strategy_name, KIND_LIQ_SWEEP)

            try:
                candidates = await self.generator.generate(session, strategy_name)
            except Exception:
                logger.exception("Pipeline: generate failed for {}", strategy_name)
                continue

            if not candidates:
                continue

            try:
                valid = await self.generator.validate(session, candidates)
            except Exception:
                logger.exception("Pipeline: validate failed for {}", strategy_name)
                continue

            if not valid:
                continue

            for candidate in valid:
                try:
                    stop_plan = self.stop_engine.place(candidate, ctx, kind=kind)
                except Exception:
                    logger.exception("StopEngine failed for candidate")
                    continue

                if stop_plan.rejected:
                    await DecisionLogger.log(
                        session=session, candidate=candidate, accepted=False,
                        ctx=ctx, rejection_reason=stop_plan.rejection_reason,
                    )
                    continue

                try:
                    target_plan = self.target_engine.place(candidate, stop_plan, ctx)
                except Exception:
                    logger.exception("TargetEngine failed for candidate")
                    continue

                # Mutate the candidate so persistence uses the engine-derived levels
                candidate = candidate.model_copy(
                    update={
                        "stop_loss": stop_plan.stop_price,
                        "take_profit_1": target_plan.tp1,
                        "take_profit_2": target_plan.tp2,
                        "risk_reward": Decimal(str(target_plan.rr_tp1)),
                    }
                )

                score = self.scoring.score(candidate, ctx, kind=kind)
                if not score.passes(self.score_threshold):
                    await DecisionLogger.log(
                        session=session, candidate=candidate, accepted=False,
                        ctx=ctx, score=score,
                        score_threshold=self.score_threshold,
                        rejection_reason=f"score_below_threshold ({score.value:.2f} < {self.score_threshold:.2f})",
                    )
                    continue

                all_scored.append((score.value, candidate, strategy_name, stop_plan, target_plan, score))

        if not all_scored:
            return []

        # 4. Pick the highest-scoring candidate across all allowed strategies
        all_scored.sort(key=lambda t: t[0], reverse=True)
        best_score, best_candidate, best_name, best_stop, best_target, best_score_obj = all_scored[0]

        # Reject the runners-up (decision-logged so we know they existed)
        for s, cand, sname, _stop, _tgt, scr in all_scored[1:]:
            await DecisionLogger.log(
                session=session, candidate=cand, accepted=False,
                ctx=ctx, score=scr, score_threshold=self.score_threshold,
                rejection_reason=f"runner_up_to_{best_name}",
            )

        # 5. Risk manager (existing) — final position-sizing check
        try:
            current_atr, baseline_atr = await self._compute_atr(session)
        except Exception:
            current_atr, baseline_atr = 1.0, 1.0
        try:
            risk_results = await self.risk_manager.check(
                session, [best_candidate],
                current_atr=current_atr, baseline_atr=baseline_atr,
            )
        except Exception:
            logger.exception("RiskManager failed; signal still persisted")
            risk_results = []

        approved = True
        approved_size: Decimal | None = None
        for cand, rr in risk_results:
            if not rr.approved:
                approved = False
                rejection = rr.rejection_reason
                await DecisionLogger.log(
                    session=session, candidate=best_candidate, accepted=False,
                    ctx=ctx, score=best_score_obj,
                    rejection_reason=f"risk_manager:{rejection}",
                )
                break
            approved_size = rr.position_size

        if not approved:
            return []

        # 6. Persist
        strat_row = (
            await session.execute(
                select(StrategyModel).where(StrategyModel.name == best_name)
            )
        ).scalar_one_or_none()
        if strat_row is None:
            logger.error("Strategy '{}' missing in strategies table", best_name)
            await DecisionLogger.log(
                session=session, candidate=best_candidate, accepted=False,
                ctx=ctx, score=best_score_obj,
                rejection_reason="strategy_row_missing",
            )
            return []

        expires_at = self.generator.compute_expiry(best_candidate)
        reasoning = best_candidate.reasoning + f" | Score {best_score:.2f}/10"
        if approved_size is not None:
            reasoning += f" | Position size: {approved_size}"

        signal = Signal(
            strategy_id=strat_row.id,
            symbol=best_candidate.symbol,
            timeframe=best_candidate.timeframe,
            direction=best_candidate.direction.value,
            entry_price=best_candidate.entry_price,
            stop_loss=best_candidate.stop_loss,
            take_profit_1=best_candidate.take_profit_1,
            take_profit_2=best_candidate.take_profit_2,
            risk_reward=best_candidate.risk_reward,
            confidence=best_candidate.confidence,
            reasoning=reasoning,
            status="active",
            expires_at=expires_at,
        )
        session.add(signal)
        await session.flush()

        await DecisionLogger.log(
            session=session, candidate=best_candidate, accepted=True,
            ctx=ctx, score=best_score_obj,
            score_threshold=self.score_threshold,
            signal_id=signal.id,
        )

        await session.commit()

        logger.info(
            "Pipeline v2: persisted {} {} score={:.2f} regime={} session={}",
            best_name, best_candidate.direction.value,
            best_score, ctx.regime.value, ctx.session.label,
        )
        return [signal]

    # ------------------------------------------------------------------
    async def _compute_atr(self, session: AsyncSession) -> tuple[float, float]:
        """Same computation as old pipeline; kept here for RiskManager input."""
        import pandas as pd
        from sqlalchemy import and_
        from app.config import get_settings
        from app.models.candle import Candle
        from app.strategies.helpers.indicators import compute_atr

        stmt = (
            select(Candle.high, Candle.low, Candle.close)
            .where(
                and_(
                    Candle.symbol == get_settings().trading_symbol,
                    Candle.timeframe == "H1",
                )
            )
            .order_by(Candle.timestamp.desc())
            .limit(100)
        )
        rows = (await session.execute(stmt)).all()
        if len(rows) < 20:
            return 1.0, 1.0
        rows = list(reversed(rows))
        highs = pd.Series([float(r[0]) for r in rows])
        lows = pd.Series([float(r[1]) for r in rows])
        closes = pd.Series([float(r[2]) for r in rows])
        atr_series = compute_atr(highs, lows, closes, length=14).dropna()
        if atr_series.empty:
            return 1.0, 1.0
        return float(atr_series.iloc[-1]), float(atr_series.mean())
