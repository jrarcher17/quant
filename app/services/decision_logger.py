"""DecisionLogger — persist every accept/reject decision with full context.

This is the single most important debug surface in the system. Every
candidate that the pipeline evaluates is recorded here, regardless of
whether it became a Signal.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.signal_decision import SignalDecision

if TYPE_CHECKING:
    from app.engines.market_context import MarketContext
    from app.engines.scoring_engine import SignalScore


class DecisionLogger:
    @staticmethod
    async def log(
        session: AsyncSession,
        *,
        candidate=None,
        accepted: bool,
        ctx: "MarketContext",
        score: "SignalScore | None" = None,
        score_threshold: float | None = None,
        rejection_reason: str | None = None,
        signal_id: int | None = None,
    ) -> None:
        try:
            # When candidate is None we're a pipeline-level gate (session /
            # news / regime). Tag it explicitly so the dashboard can group
            # those rows separately from per-strategy decisions.
            strategy_name = (
                getattr(candidate, "strategy_name", None) or "(pipeline gate)"
            )
            direction = None
            entry_price = None
            if candidate is not None:
                direction = (
                    candidate.direction.value
                    if hasattr(candidate.direction, "value")
                    else str(candidate.direction)
                )
                entry_price = Decimal(str(candidate.entry_price))

            row = SignalDecision(
                signal_id=signal_id,
                strategy_name=strategy_name,
                direction=direction,
                entry_price=entry_price,
                accepted=accepted,
                score=Decimal(str(score.value)) if score else None,
                score_threshold=Decimal(str(score_threshold)) if score_threshold else None,
                regime=ctx.regime.value,
                htf_bias=ctx.htf_bias.value,
                session=ctx.session.label,
                news_blocked=ctx.news.blocked,
                rejection_reason=rejection_reason,
                score_breakdown=(
                    {"value": score.value, "rationale": score.rationale}
                    if score else None
                ),
                context_snapshot={
                    **ctx.as_snapshot(),
                    "liquidity": ctx.liquidity.as_snapshot(),
                },
            )
            session.add(row)
        except Exception:
            logger.exception("DecisionLogger: failed to record decision")
