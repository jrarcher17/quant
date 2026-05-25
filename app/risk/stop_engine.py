"""StopEngine — structural stop placement.

Places stops past the *real* invalidation level (swing, swept liquidity,
range edge) plus a buffer that keeps it clear of obvious round-number
clusters. Returns a StopPlan that the pipeline either accepts or rejects.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from app.engines.market_context import MarketContext


# Pad past structural level to clear stop clusters
_ROUND_NUMBER_BUFFER_PTS = Decimal("5.0")
_ATR_CUSHION_MULT = Decimal("0.30")


@dataclass
class StopPlan:
    stop_price: Decimal
    risk_pts: Decimal
    structure_anchor: float | None
    reason: str
    rejected: bool = False
    rejection_reason: str | None = None


class StopEngine:
    """Computes a structural stop loss for a candidate."""

    def __init__(self, max_risk_pts: float = 80.0) -> None:
        # Hard cap: in points (= dollars per oz on XAUUSD)
        self.max_risk_pts = Decimal(str(max_risk_pts))

    def place(
        self,
        candidate,
        ctx: "MarketContext",
        *,
        kind: str,
    ) -> StopPlan:
        direction = candidate.direction.value if hasattr(candidate.direction, "value") else str(candidate.direction)
        entry = Decimal(str(candidate.entry_price))
        atr_dec = Decimal(str(max(ctx.atr, 0.0))) if ctx.atr else Decimal("0")
        cushion = max(_ATR_CUSHION_MULT * atr_dec, _ROUND_NUMBER_BUFFER_PTS)

        anchor = self._find_structure_anchor(candidate, ctx, direction, kind)
        if anchor is None:
            # Fallback: use the candidate's own SL (already ATR-based)
            sl_dec = Decimal(str(candidate.stop_loss))
            risk = (entry - sl_dec).copy_abs()
            return self._finalise(
                StopPlan(
                    stop_price=sl_dec,
                    risk_pts=risk,
                    structure_anchor=None,
                    reason="no_structure_fallback_to_atr",
                )
            )

        anchor_dec = Decimal(str(anchor))
        if direction == "BUY":
            sl = anchor_dec - cushion
        else:
            sl = anchor_dec + cushion

        risk_pts = (entry - sl).copy_abs()

        return self._finalise(
            StopPlan(
                stop_price=sl.quantize(Decimal("0.01")),
                risk_pts=risk_pts.quantize(Decimal("0.01")),
                structure_anchor=float(anchor_dec),
                reason=f"struct_anchor={anchor_dec:.2f}_buffer={cushion:.2f}",
            )
        )

    def _finalise(self, plan: StopPlan) -> StopPlan:
        if plan.risk_pts > self.max_risk_pts:
            plan.rejected = True
            plan.rejection_reason = (
                f"structural risk {plan.risk_pts:.2f} pts exceeds cap {self.max_risk_pts:.2f}"
            )
            logger.info("StopEngine: REJECT — {}", plan.rejection_reason)
        elif plan.risk_pts <= 0:
            plan.rejected = True
            plan.rejection_reason = "non-positive risk"
        return plan

    def _find_structure_anchor(
        self, candidate, ctx: "MarketContext", direction: str, kind: str
    ) -> float | None:
        """Pick the most recent invalidating level for this trade."""
        from app.engines.scoring_engine import KIND_BREAKOUT, KIND_LIQ_SWEEP

        sl_existing = float(candidate.stop_loss)

        # Liquidity sweep: stop should sit beyond the swept wick — already in candidate.stop_loss
        if kind == KIND_LIQ_SWEEP:
            return sl_existing

        # Breakout: stop at opposite edge of consolidation range — already in candidate.stop_loss
        if kind == KIND_BREAKOUT:
            return sl_existing

        # Trend cont / momentum: snap to most recent swing
        liq = ctx.liquidity
        if direction == "BUY":
            candidates = list(liq.swing_lows) + [liq.pdl, liq.asia_low, liq.london_low]
            below = [v for v in candidates if v is not None and v < float(candidate.entry_price)]
            if below:
                return max(below)
        else:
            candidates = list(liq.swing_highs) + [liq.pdh, liq.asia_high, liq.london_high]
            above = [v for v in candidates if v is not None and v > float(candidate.entry_price)]
            if above:
                return min(above)

        return sl_existing  # fallback to strategy's own SL
