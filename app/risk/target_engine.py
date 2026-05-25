"""TargetEngine — dynamic, liquidity-aware TP placement.

Rules:
    TP1 = always 1.0 R (locks in breakeven; original 50% partial close logic
          in paper_broker assumes TP1 < TP2 same direction).
    TP2 = nearest opposing liquidity level (PDH/PDL/swing/etc.) in trade
          direction, OR fallback 2.5 R, whichever closer.

If the structural target gives < 1.5 R, fall back to a fixed 2.5 R so the
trade has meaningful upside.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from app.engines.market_context import MarketContext
    from app.risk.stop_engine import StopPlan


@dataclass
class TargetPlan:
    tp1: Decimal
    tp2: Decimal
    rr_tp1: float
    rr_tp2: float
    tp2_source: str  # "structure" | "fallback_rr"


class TargetEngine:
    TP1_RR = Decimal("1.0")
    TP2_FALLBACK_RR = Decimal("2.5")
    MIN_TP2_RR = Decimal("1.5")

    def place(
        self,
        candidate,
        stop: "StopPlan",
        ctx: "MarketContext",
    ) -> TargetPlan:
        direction = candidate.direction.value if hasattr(candidate.direction, "value") else str(candidate.direction)
        entry = Decimal(str(candidate.entry_price))
        risk = stop.risk_pts

        if direction == "BUY":
            tp1 = entry + self.TP1_RR * risk
        else:
            tp1 = entry - self.TP1_RR * risk

        # Hunt for structural target
        tp2_struct = ctx.liquidity.nearest_target_pts(float(entry), direction)
        tp2_source = "fallback_rr"

        if tp2_struct is not None:
            tp2_struct_dec = Decimal(str(tp2_struct))
            tp2_rr_struct = tp2_struct_dec / risk if risk > 0 else Decimal("0")
            if tp2_rr_struct >= self.MIN_TP2_RR:
                if direction == "BUY":
                    tp2 = entry + tp2_struct_dec
                else:
                    tp2 = entry - tp2_struct_dec
                tp2_source = "structure"
            else:
                if direction == "BUY":
                    tp2 = entry + self.TP2_FALLBACK_RR * risk
                else:
                    tp2 = entry - self.TP2_FALLBACK_RR * risk
        else:
            if direction == "BUY":
                tp2 = entry + self.TP2_FALLBACK_RR * risk
            else:
                tp2 = entry - self.TP2_FALLBACK_RR * risk

        rr_tp1 = float((tp1 - entry).copy_abs() / risk) if risk > 0 else 0.0
        rr_tp2 = float((tp2 - entry).copy_abs() / risk) if risk > 0 else 0.0

        plan = TargetPlan(
            tp1=tp1.quantize(Decimal("0.01")),
            tp2=tp2.quantize(Decimal("0.01")),
            rr_tp1=round(rr_tp1, 2),
            rr_tp2=round(rr_tp2, 2),
            tp2_source=tp2_source,
        )

        logger.debug(
            "TargetEngine: entry={} risk={:.2f} tp1={}({}R) tp2={}({}R) source={}",
            entry, risk, plan.tp1, plan.rr_tp1, plan.tp2, plan.rr_tp2, tp2_source,
        )
        return plan
