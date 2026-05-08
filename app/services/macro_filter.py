"""Macro bias filter for gold trading signals.

Computes a directional macro bias from two sources available via the
existing Candle table:

    DXY trend   -- 10-day percent change in DXY D1 close.
                   Rising DXY → dollar strength → bearish gold pressure.
                   Falling DXY → dollar weakness → bullish gold pressure.

    VIX level   -- current CBOE Volatility Index value.
                   High VIX signals risk-off; gold benefits as a safe haven.
                   A sharp VIX spike amplifies the bullish signal further.

Each source contributes a numeric score. The combined score determines the
bias direction and strength:

    score >=  20  →  BULLISH  (favour BUY signals)
    score <= -15  →  BEARISH  (favour SELL signals; only DXY can push here)
    else          →  NEUTRAL  (no macro view; signals unmodified)

Confidence adjustments applied to candidates (non-blocking):

    Aligned signal:  +MACRO_ALIGNED_BOOST  (default +5 pts)
    Opposed signal:  -MACRO_OPPOSED_PENALTY (default -15 pts)

All DB queries degrade gracefully: if data is unavailable the filter
returns NEUTRAL and leaves signals unchanged. This method never raises.

Exports:
    MacroBiasDirection  -- BULLISH / BEARISH / NEUTRAL enum
    MacroBias           -- frozen result dataclass
    MacroBiasFilter     -- service class
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candle import Candle
from app.strategies.base import CandidateSignal


# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

DXY_SYMBOL: str = "DXY"
VIX_SYMBOL: str = "VIX"

# DXY: 10-day momentum window
DXY_LOOKBACK: int = 25
DXY_MIN_CANDLES: int = 12
DXY_MOMENTUM_DAYS: int = 10

DXY_STRONG_PCT: float = 1.0    # >= 1% 10-day move  → strong signal
DXY_MODERATE_PCT: float = 0.5  # >= 0.5% 10-day move → moderate signal

DXY_STRONG_SCORE: float = 30.0
DXY_MODERATE_SCORE: float = 15.0

# VIX: level + spike within a rolling window
VIX_LOOKBACK: int = 7
VIX_MIN_CANDLES: int = 2

VIX_PANIC_LEVEL: float = 30.0    # > 30: panic / risk-off flight to safety
VIX_HIGH_LEVEL: float = 20.0     # 20-30: elevated fear
VIX_MILD_LEVEL: float = 15.0     # 15-20: mild caution

VIX_PANIC_SCORE: float = 30.0
VIX_HIGH_SCORE: float = 15.0
VIX_MILD_SCORE: float = 5.0

VIX_STRONG_SPIKE: float = 10.0      # VIX spike vs oldest-in-window
VIX_MODERATE_SPIKE: float = 5.0

VIX_STRONG_SPIKE_SCORE: float = 15.0
VIX_MODERATE_SPIKE_SCORE: float = 8.0
VIX_MAX_SCORE: float = 40.0          # cap so VIX alone can't dominate

# Combined thresholds
BIAS_BULLISH_THRESHOLD: float = 20.0
BIAS_BEARISH_THRESHOLD: float = -15.0

# Confidence delta applied in apply()
MACRO_ALIGNED_BOOST: float = 5.0
MACRO_OPPOSED_PENALTY: float = 15.0


# ---------------------------------------------------------------------------
# Enums and data classes
# ---------------------------------------------------------------------------


class MacroBiasDirection(str, Enum):
    """Directional macro bias for gold."""

    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class MacroBias:
    """Result of the macro bias computation.

    Attributes:
        direction: The computed bias direction.
        strength:  Absolute magnitude of the combined score (0-100).
        signals:   Human-readable descriptions of contributing signals.
        available: False when no macro data exists (e.g. first boot).
        message:   Short summary for logging / reasoning strings.
    """

    direction: MacroBiasDirection
    strength: float
    signals: list[str]
    available: bool
    message: str


# ---------------------------------------------------------------------------
# MacroBiasFilter
# ---------------------------------------------------------------------------


class MacroBiasFilter:
    """Compute and apply a macro directional bias to gold candidate signals.

    Usage in the pipeline::

        filter = MacroBiasFilter()
        bias = await filter.compute_bias(session)
        candidates = filter.apply(candidates, bias)

    ``compute_bias`` queries the Candle table for DXY and VIX D1 data.
    Both are ingested via the ``refresh_macro_symbols`` scheduled job
    (daily at 00:05 UTC) and bootstrapped on first deploy.

    ``apply`` is synchronous and side-effect free -- it returns a new list
    of ``CandidateSignal`` instances with updated confidence and reasoning.
    The original list is never mutated.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def compute_bias(self, session: AsyncSession) -> MacroBias:
        """Compute the current macro bias from DXY and VIX.

        Returns a NEUTRAL, unavailable ``MacroBias`` when no data exists.
        This method never raises.
        """
        _unavailable = MacroBias(
            direction=MacroBiasDirection.NEUTRAL,
            strength=0.0,
            signals=[],
            available=False,
            message="Macro data unavailable",
        )

        try:
            signal_parts: list[str] = []
            total_score: float = 0.0

            dxy_score, dxy_msg = await self._dxy_score(session)
            if dxy_msg is not None:
                total_score += dxy_score
                signal_parts.append(dxy_msg)

            vix_score, vix_msg = await self._vix_score(session)
            if vix_msg is not None:
                total_score += vix_score
                signal_parts.append(vix_msg)

            if not signal_parts:
                logger.info("Macro bias: no signal data available -- NEUTRAL")
                return _unavailable

            if total_score >= BIAS_BULLISH_THRESHOLD:
                direction = MacroBiasDirection.BULLISH
            elif total_score <= BIAS_BEARISH_THRESHOLD:
                direction = MacroBiasDirection.BEARISH
            else:
                direction = MacroBiasDirection.NEUTRAL

            strength = min(abs(total_score), 100.0)
            msg = f"Macro bias={direction.value} score={total_score:.1f}"
            logger.info("{} | signals: {}", msg, signal_parts)

            return MacroBias(
                direction=direction,
                strength=strength,
                signals=signal_parts,
                available=True,
                message=msg,
            )

        except Exception:
            logger.opt(exception=True).warning(
                "MacroBiasFilter.compute_bias failed -- degrading to NEUTRAL"
            )
            return _unavailable

    def apply(
        self,
        candidates: list[CandidateSignal],
        bias: MacroBias,
    ) -> list[CandidateSignal]:
        """Adjust candidate confidence based on macro alignment.

        No changes are made when the bias is unavailable or NEUTRAL.

        Adjustments:
            Aligned  (signal direction == bias direction): +MACRO_ALIGNED_BOOST
            Opposed  (signal direction != bias direction): -MACRO_OPPOSED_PENALTY

        Confidence is clamped to [0, 100] after adjustment.

        Args:
            candidates: List of candidates to adjust (usually one element).
            bias:       Result of ``compute_bias``.

        Returns:
            New list of ``CandidateSignal`` with updated confidence and
            appended reasoning tag. The input list is not mutated.
        """
        if not bias.available or bias.direction == MacroBiasDirection.NEUTRAL:
            return candidates

        adjusted: list[CandidateSignal] = []
        for candidate in candidates:
            sig_dir = candidate.direction.value  # "BUY" or "SELL"
            aligned = (
                sig_dir == "BUY" and bias.direction == MacroBiasDirection.BULLISH
            ) or (
                sig_dir == "SELL" and bias.direction == MacroBiasDirection.BEARISH
            )

            current_conf = float(candidate.confidence)
            if aligned:
                new_conf = min(current_conf + MACRO_ALIGNED_BOOST, 100.0)
                tag = (
                    f"Macro {bias.direction.value} aligned: "
                    f"+{MACRO_ALIGNED_BOOST:.0f} conf"
                )
            else:
                new_conf = max(current_conf - MACRO_OPPOSED_PENALTY, 0.0)
                tag = (
                    f"Macro {bias.direction.value} opposes {sig_dir}: "
                    f"-{MACRO_OPPOSED_PENALTY:.0f} conf"
                )

            adjusted.append(
                candidate.model_copy(
                    update={
                        "confidence": Decimal(str(round(new_conf, 2))),
                        "reasoning": candidate.reasoning + f" | {tag}",
                    }
                )
            )
            logger.info(
                "Macro bias applied | strategy={} dir={} bias={} "
                "aligned={} conf {:.1f} -> {:.1f}",
                candidate.strategy_name,
                sig_dir,
                bias.direction.value,
                aligned,
                current_conf,
                new_conf,
            )

        return adjusted

    # ------------------------------------------------------------------
    # Internal: DXY score
    # ------------------------------------------------------------------

    async def _dxy_score(
        self, session: AsyncSession
    ) -> tuple[float, str | None]:
        """Compute the DXY contribution to the macro score.

        Measures the 10-day percent change in DXY D1 close prices.
        Rising DXY → negative score (dollar strength, bearish for gold).
        Falling DXY → positive score (dollar weakness, bullish for gold).

        Returns:
            (score, description) -- score is 0.0 and description is None
            when data is insufficient.
        """
        stmt = (
            select(Candle.close)
            .where(Candle.symbol == DXY_SYMBOL, Candle.timeframe == "D1")
            .order_by(Candle.timestamp.desc())
            .limit(DXY_LOOKBACK)
        )
        result = await session.execute(stmt)
        rows = result.all()

        if len(rows) < DXY_MIN_CANDLES:
            logger.debug(
                "DXY bias: insufficient candles ({}/{}), skipping",
                len(rows),
                DXY_MIN_CANDLES,
            )
            return 0.0, None

        # Rows are DESC; reverse to chronological order (oldest first)
        closes = [float(r[0]) for r in reversed(rows)]
        today_close = closes[-1]
        past_close = closes[-(DXY_MOMENTUM_DAYS + 1)]

        if past_close == 0:
            return 0.0, None

        pct_change = (today_close - past_close) / past_close * 100.0

        if pct_change >= DXY_STRONG_PCT:
            score = -DXY_STRONG_SCORE
            label = (
                f"DXY +{pct_change:.2f}% ({DXY_MOMENTUM_DAYS}d) "
                f"→ strong bearish gold"
            )
        elif pct_change >= DXY_MODERATE_PCT:
            score = -DXY_MODERATE_SCORE
            label = (
                f"DXY +{pct_change:.2f}% ({DXY_MOMENTUM_DAYS}d) "
                f"→ bearish gold"
            )
        elif pct_change <= -DXY_STRONG_PCT:
            score = DXY_STRONG_SCORE
            label = (
                f"DXY {pct_change:.2f}% ({DXY_MOMENTUM_DAYS}d) "
                f"→ strong bullish gold"
            )
        elif pct_change <= -DXY_MODERATE_PCT:
            score = DXY_MODERATE_SCORE
            label = (
                f"DXY {pct_change:.2f}% ({DXY_MOMENTUM_DAYS}d) "
                f"→ bullish gold"
            )
        else:
            score = 0.0
            label = f"DXY {pct_change:.2f}% ({DXY_MOMENTUM_DAYS}d) → neutral"

        logger.debug(
            "DXY score={} pct_change={:.3f}% today={:.3f} past={:.3f}",
            score,
            pct_change,
            today_close,
            past_close,
        )
        return score, label

    # ------------------------------------------------------------------
    # Internal: VIX score
    # ------------------------------------------------------------------

    async def _vix_score(
        self, session: AsyncSession
    ) -> tuple[float, str | None]:
        """Compute the VIX contribution to the macro score.

        High VIX = risk-off = safe-haven demand = bullish for gold.
        A sharp spike in VIX amplifies the signal further.

        Score components:
            Level: absolute VIX value mapped to a fixed score tier.
            Spike: change in VIX vs the oldest candle in the lookback window.

        Returns:
            (score, description) -- score is 0.0 and description is None
            when data is insufficient.
        """
        stmt = (
            select(Candle.close)
            .where(Candle.symbol == VIX_SYMBOL, Candle.timeframe == "D1")
            .order_by(Candle.timestamp.desc())
            .limit(VIX_LOOKBACK)
        )
        result = await session.execute(stmt)
        rows = result.all()

        if len(rows) < VIX_MIN_CANDLES:
            logger.debug(
                "VIX bias: insufficient candles ({}/{}), skipping",
                len(rows),
                VIX_MIN_CANDLES,
            )
            return 0.0, None

        # Rows are DESC; reverse to chronological order (oldest first)
        closes = [float(r[0]) for r in reversed(rows)]
        latest_vix = closes[-1]

        # Level component
        if latest_vix >= VIX_PANIC_LEVEL:
            level_score = VIX_PANIC_SCORE
            level_label = f"VIX={latest_vix:.1f} (panic/risk-off)"
        elif latest_vix >= VIX_HIGH_LEVEL:
            level_score = VIX_HIGH_SCORE
            level_label = f"VIX={latest_vix:.1f} (elevated fear)"
        elif latest_vix >= VIX_MILD_LEVEL:
            level_score = VIX_MILD_SCORE
            level_label = f"VIX={latest_vix:.1f} (mild caution)"
        else:
            level_score = 0.0
            level_label = f"VIX={latest_vix:.1f} (low fear)"

        # Spike component (latest vs oldest in window)
        spike = latest_vix - closes[0]
        if spike >= VIX_STRONG_SPIKE:
            spike_score = VIX_STRONG_SPIKE_SCORE
            spike_label = f"+{spike:.1f}pt VIX spike"
        elif spike >= VIX_MODERATE_SPIKE:
            spike_score = VIX_MODERATE_SPIKE_SCORE
            spike_label = f"+{spike:.1f}pt VIX spike"
        else:
            spike_score = 0.0
            spike_label = ""

        total_vix = min(level_score + spike_score, VIX_MAX_SCORE)

        parts = [level_label]
        if spike_label:
            parts.append(spike_label)
        label = "; ".join(parts)

        logger.debug(
            "VIX score={} level={} spike={} latest={:.1f}",
            total_vix,
            level_score,
            spike_score,
            latest_vix,
        )
        return total_vix, label
