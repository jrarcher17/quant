"""RegimeStrategySelector — picks which strategies are allowed to fire.

Routes regime → allowed strategy list per the design spec:

    STRONG_TREND  → trend_continuation, ema_momentum
    COMPRESSION   → breakout_expansion
    EXHAUSTION    → liquidity_sweep
    CHOP          → []  (no signals)
    UNKNOWN       → []
"""

from __future__ import annotations

from loguru import logger

from app.engines.regime_engine import Regime


_REGIME_ALLOWLIST: dict[Regime, list[str]] = {
    Regime.STRONG_TREND: ["trend_continuation", "ema_momentum"],
    Regime.COMPRESSION: ["breakout_expansion"],
    Regime.EXHAUSTION: ["liquidity_sweep"],
    Regime.CHOP: [],
    Regime.UNKNOWN: [],
}


class RegimeStrategySelector:
    def allowed(self, regime: Regime) -> list[str]:
        names = _REGIME_ALLOWLIST.get(regime, [])
        logger.debug("RegimeStrategySelector: regime={} -> {}", regime.value, names)
        return list(names)
