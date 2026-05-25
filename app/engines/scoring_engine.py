"""ScoringEngine — composite 0..10 score for each candidate signal.

Replaces the old additive +10 confidence bonuses. The score combines:
    - HTF alignment (max +2.0, penalty -2.0)
    - Session quality (0..2.0)
    - Range/volatility expansion (max +1.5)
    - ADX strength for trend setups (max +1.0)
    - Liquidity proximity penalty (-1.5..+0.5)
    - Regime fit bonus (+1.0)
    - Volatility-regime sanity (+0.5..-0.5)

Strategies still produce candidates with a "kind" tag; the engine looks at
that tag to apply the right bonus rules (e.g. ADX bonus only for trend
setups).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from loguru import logger

from app.engines.htf_bias_engine import HTFBias
from app.engines.regime_engine import Regime

if TYPE_CHECKING:
    from app.engines.market_context import MarketContext

# Setup classification used by the scorer
KIND_LIQ_SWEEP = "liq_sweep"
KIND_TREND_CONT = "trend_cont"
KIND_BREAKOUT = "breakout"
KIND_EMA_MOM = "ema_momentum"


@dataclass
class SignalScore:
    value: float
    rationale: list[str] = field(default_factory=list)

    def passes(self, threshold: float) -> bool:
        return self.value >= threshold


class ScoringEngine:
    DEFAULT_THRESHOLD = 6.5

    def score(self, candidate, ctx: "MarketContext", *, kind: str) -> SignalScore:
        s = 0.0
        rationale: list[str] = []

        direction = candidate.direction.value if hasattr(candidate.direction, "value") else str(candidate.direction)
        is_buy = direction == "BUY"

        # 1. HTF alignment ---------------------------------------------------
        bias = ctx.htf_bias
        if bias == HTFBias.LONG_ONLY:
            if is_buy:
                s += 2.0; rationale.append("htf_long+2.0")
            else:
                s -= 2.0; rationale.append("htf_counter-2.0")
        elif bias == HTFBias.SHORT_ONLY:
            if not is_buy:
                s += 2.0; rationale.append("htf_short+2.0")
            else:
                s -= 2.0; rationale.append("htf_counter-2.0")
        elif bias == HTFBias.LEAN_LONG:
            if is_buy:
                s += 1.0; rationale.append("htf_lean_long+1.0")
            else:
                s -= 1.0; rationale.append("htf_lean_against-1.0")
        elif bias == HTFBias.LEAN_SHORT:
            if not is_buy:
                s += 1.0; rationale.append("htf_lean_short+1.0")
            else:
                s -= 1.0; rationale.append("htf_lean_against-1.0")
        else:
            s -= 0.5; rationale.append("htf_neutral-0.5")

        # 2. Session quality (0..2.0) ---------------------------------------
        sq = ctx.session.quality_multiplier * 2.0
        s += sq
        rationale.append(f"session({ctx.session.label})+{sq:.2f}")

        # 3. Range/volatility expansion -------------------------------------
        if ctx.atr_avg > 0 and ctx.last_bar_range > 0:
            ratio = ctx.last_bar_range / ctx.atr_avg
            if ratio >= 1.5:
                s += 1.5; rationale.append(f"range_exp+1.5({ratio:.2f}x)")
            elif ratio >= 1.2:
                s += 0.75; rationale.append(f"range_exp+0.75({ratio:.2f}x)")
            elif ratio < 0.6:
                s -= 0.5; rationale.append(f"range_compress-0.5({ratio:.2f}x)")

        # 4. ADX strength for trend setups ----------------------------------
        if kind in (KIND_TREND_CONT, KIND_EMA_MOM):
            if ctx.adx >= 30:
                s += 1.0; rationale.append(f"adx+1.0({ctx.adx:.1f})")
            elif ctx.adx >= 25:
                s += 0.5; rationale.append(f"adx+0.5({ctx.adx:.1f})")
            elif ctx.adx < 20:
                s -= 1.0; rationale.append(f"adx-1.0({ctx.adx:.1f})")

        # 5. Liquidity proximity --------------------------------------------
        try:
            entry = float(candidate.entry_price)
            obstacle = ctx.liquidity.nearest_obstacle_pts(entry, direction)
            if obstacle is not None and ctx.atr > 0:
                if obstacle < 0.5 * ctx.atr:
                    s -= 1.5; rationale.append(f"liq_into_face-1.5({obstacle:.1f}pts)")
                elif obstacle > 2.0 * ctx.atr:
                    s += 0.5; rationale.append(f"clear_runway+0.5({obstacle:.1f}pts)")
        except Exception:
            pass

        # 6. Regime fit ------------------------------------------------------
        regime_fit = (
            (kind == KIND_LIQ_SWEEP and ctx.regime == Regime.EXHAUSTION)
            or (kind in (KIND_TREND_CONT, KIND_EMA_MOM) and ctx.regime == Regime.STRONG_TREND)
            or (kind == KIND_BREAKOUT and ctx.regime == Regime.COMPRESSION)
        )
        if regime_fit:
            s += 1.0; rationale.append(f"regime_fit+1.0({ctx.regime.value})")
        elif ctx.regime == Regime.CHOP:
            s -= 1.0; rationale.append("regime_chop-1.0")

        # 7. Volatility regime sanity ---------------------------------------
        if 0.30 <= ctx.atr_percentile <= 0.70:
            s += 0.5; rationale.append(f"vol_normal+0.5({ctx.atr_percentile:.2f})")
        elif ctx.atr_percentile > 0.90:
            s -= 0.5; rationale.append(f"vol_spike-0.5({ctx.atr_percentile:.2f})")
        elif ctx.atr_percentile < 0.10:
            s -= 0.25; rationale.append(f"vol_dead-0.25({ctx.atr_percentile:.2f})")

        # Clip to [0, 10]
        s_clipped = max(0.0, min(10.0, s))
        score = SignalScore(value=round(s_clipped, 2), rationale=rationale)

        logger.debug(
            "ScoringEngine: {} {} -> score={:.2f} | {}",
            kind, direction, score.value, ", ".join(rationale),
        )
        return score
