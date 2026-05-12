"""Signal generator service: generation, validation, dedup, expiry, and bias detection.

Transforms strategy analysis into validated, de-duplicated trade signals.
Float math internally; Decimal(str(round(x, 2))) at persistence boundary only.
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.candle import Candle
from app.models.signal import Signal
from app.services.trade_settings import get_trade_settings
from app.strategies.base import get_pip_value as _get_pip_value

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

MIN_RR: float = 1.3  # Minimum risk:reward ratio (1:1.3)
MIN_CONFIDENCE: float = 60.0  # Minimum confidence threshold (%)
MAX_SL_PIPS: float = 800.0  # Max stop loss distance (pips); ~$80 ≈ 1.5% at XAU $5,300
DEDUP_WINDOW_HOURS: int = 2  # Same-direction dedup window (hours)
# Statuses counted for dedup — includes closed signals to prevent re-entry
# into the same direction immediately after a loss
DEDUP_STATUSES: tuple[str, ...] = ("active", "sl_hit", "tp_hit", "expired")
EXPIRY_HOURS: dict[str, int] = {
    "M15": 4,   # Scalp: 4 hours
    "H1": 8,    # Intraday: 8 hours
    "H4": 24,   # Intraday/swing: 24 hours
    "D1": 48,   # Swing: 48 hours
}
BIAS_WINDOW_SIGNALS: int = 20  # Number of recent signals to check for bias

# Per-strategy safety limits
MAX_SIGNALS_PER_STRATEGY_DAY: int = 2     # Max signals per strategy per calendar day
STRATEGY_LOSS_STREAK_LIMIT: int = 3       # Consecutive SL hits that trigger cooldown
STRATEGY_COOLDOWN_HOURS: int = 4          # Hours to block a strategy after streak
BIAS_SKEW_THRESHOLD: float = 0.75  # >75% same direction flags bias


class SignalGenerator:
    """Generates, validates, deduplicates, and expires trade signals.

    Usage (within pipeline orchestrator)::

        sg = SignalGenerator()
        candidates = await sg.generate(session, "liquidity_sweep")
        validated = await sg.validate(session, candidates)
        # Persist validated signals in pipeline (Plan 05)
    """

    async def generate(
        self,
        session: AsyncSession,
        strategy_name: str,
    ) -> list:
        """Run a strategy's analyze() on latest candle data.

        Imports strategy modules inside the method body to trigger
        auto-registration and avoid circular imports (Phase 3 pattern).

        Args:
            session: Async database session.
            strategy_name: Registered strategy name (e.g. "liquidity_sweep").

        Returns:
            List of CandidateSignal instances (may be empty).
        """
        # --- Lazy imports (circular-import avoidance, Phase 3 pattern) ---
        from app.strategies.base import (
            BaseStrategy,
            CandidateSignal,
            InsufficientDataError,
            candles_to_dataframe,
        )
        # Import concrete strategies to trigger registration
        import app.strategies.liquidity_sweep  # noqa: F401
        import app.strategies.trend_continuation  # noqa: F401
        import app.strategies.breakout_expansion  # noqa: F401
        import app.strategies.ema_momentum  # noqa: F401

        # 1. Get strategy instance (with optimized params and trade settings if available)
        opt_params = await self._load_optimized_params(session, strategy_name)
        trade_settings = await get_trade_settings(session)
        registry = BaseStrategy.get_registry()
        strategy_cls = registry.get(strategy_name)
        if strategy_cls is None:
            logger.error(
                "Strategy '{}' not found in registry. Available: {}",
                strategy_name,
                list(registry.keys()),
            )
            return []
        params = dict(opt_params or {})
        for key, value in {
            "TP1_RR": trade_settings.tp1_rr,
            "TP2_RR": trade_settings.tp2_rr,
            "MAX_SL_PIPS": trade_settings.max_sl_pips,
        }.items():
            if key in strategy_cls.DEFAULT_PARAMS:
                params[key] = value

        strategy = BaseStrategy.get_strategy(strategy_name, params=params)

        if params:
            logger.info(
                "Using params for '{}': {}",
                strategy_name,
                {k: v for k, v in params.items()
                 if v != strategy_cls.DEFAULT_PARAMS.get(k)},
            )
        else:
            logger.debug("Using default params for '{}'", strategy_name)

        # 2. Query latest candles for the strategy's primary timeframe
        primary_tf = strategy.required_timeframes[0]
        limit = strategy.min_candles + 50  # Extra buffer
        ts_settings = await get_trade_settings(session)
        symbol = ts_settings.trading_symbol or get_settings().trading_symbol

        stmt = (
            select(Candle)
            .where(
                and_(
                    Candle.symbol == symbol,
                    Candle.timeframe == primary_tf,
                )
            )
            .order_by(Candle.timestamp.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        candles = result.scalars().all()

        if not candles:
            logger.warning(
                "No candles found for {}/{} -- cannot generate signals",
                symbol,
                primary_tf,
            )
            return []

        # 3. Convert to DataFrame (sorts ascending internally)
        df = candles_to_dataframe(list(candles))

        # 4. Run strategy analysis
        try:
            candidates: list[CandidateSignal] = strategy.analyze(df, symbol=symbol)
        except InsufficientDataError as exc:
            logger.warning(
                "Insufficient data for strategy '{}': {}",
                strategy_name,
                exc,
            )
            return []

        for candidate in candidates:
            candidate.symbol = symbol

        # 5. Filter stale candidates -- only keep signals from recent candles.
        # Strategies scan the entire lookback window and produce signals for
        # historical bars where patterns occurred. But those entry prices are
        # stale -- the market has moved, so the outcome detector would
        # instantly trigger TP/SL. Only accept signals from the last 3 bars.
        if candidates:
            # Staleness cutoff: 2x the timeframe interval (was 3x).
            # Strategies scan a full lookback window and can emit signals for
            # historical bars. Keeping only the last 2 bars' worth of signals
            # ensures the entry price is still actionable and avoids immediately
            # triggering TP/SL on a stale setup.
            tf_hours = {"M15": 0.25, "H1": 1, "H4": 4, "D1": 24}
            interval_hours = tf_hours.get(primary_tf, 1)
            staleness_cutoff = datetime.now(timezone.utc) - timedelta(
                hours=interval_hours * 2
            )

            fresh = []
            stale_count = 0
            for c in candidates:
                c_ts = c.timestamp
                if c_ts is not None:
                    # Handle naive timestamps
                    if c_ts.tzinfo is None:
                        c_ts = c_ts.replace(tzinfo=timezone.utc)
                    if c_ts >= staleness_cutoff:
                        fresh.append(c)
                    else:
                        stale_count += 1
                else:
                    fresh.append(c)  # no timestamp = keep

            if stale_count > 0:
                logger.info(
                    "Strategy '{}': filtered {} stale candidates "
                    "(older than {}), {} fresh remain",
                    strategy_name,
                    stale_count,
                    staleness_cutoff.isoformat(),
                    len(fresh),
                )
            candidates = fresh

        if candidates:
            logger.info(
                "Strategy '{}' produced {} candidate signal(s)",
                strategy_name,
                len(candidates),
            )
        else:
            logger.info(
                "Strategy '{}' produced 0 candidates from {} candles "
                "(timeframe={}, scanned last ~{} bars)",
                strategy_name,
                len(df),
                primary_tf,
                max(0, len(df) - strategy.min_candles),
            )
        return candidates

    @staticmethod
    async def _load_optimized_params(
        session: AsyncSession,
        strategy_name: str,
    ) -> dict[str, float] | None:
        """Load active, non-overfitted optimized params for a strategy.

        Returns the params dict if found, or None to use defaults.
        Gracefully returns None if the optimized_params table doesn't exist.
        """
        try:
            from app.models.optimized_params import OptimizedParams

            stmt = (
                select(OptimizedParams.params)
                .where(
                    and_(
                        OptimizedParams.strategy_name == strategy_name,
                        OptimizedParams.is_active.is_(True),
                        OptimizedParams.is_overfitted.isnot(True),
                    )
                )
                .order_by(OptimizedParams.created_at.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return row if row else None
        except Exception as exc:
            logger.warning(
                "Could not load optimized params for '{}': {} -- using defaults",
                strategy_name,
                str(exc)[:120],
            )
            # Rollback the failed transaction so the session is usable
            await session.rollback()
            return None

    async def validate(
        self,
        session: AsyncSession,
        candidates: list,
    ) -> list:
        """Apply validation filters to candidate signals.

        Filters applied in order:
          1. R:R >= MIN_RR  (reject below)
          2. Confidence >= MIN_CONFIDENCE  (reject below)
          3. Dedup check within DEDUP_WINDOW_HOURS  (suppress duplicates)
          4. Directional bias check  (warn only, does not reject)

        Args:
            session: Async database session.
            candidates: List of CandidateSignal instances.

        Returns:
            Filtered list of CandidateSignal instances that passed all filters.
        """
        validated: list = []
        trade_settings = await get_trade_settings(session)

        for candidate in candidates:
            # --- Filter 0: SL direction sanity check ---
            # BUY: SL must be strictly below entry; SELL: SL must be strictly above.
            # A reversed SL causes the outcome detector to fire immediately and
            # indicates a strategy calculation error for this specific bar.
            entry_f = float(candidate.entry_price)
            sl_f = float(candidate.stop_loss)
            direction_str = candidate.direction.value
            if direction_str == "BUY" and sl_f >= entry_f:
                logger.warning(
                    "Signal rejected: BUY SL {:.2f} >= entry {:.2f} "
                    "(inverted stop) from '{}'",
                    sl_f,
                    entry_f,
                    candidate.strategy_name,
                )
                continue
            if direction_str == "SELL" and sl_f <= entry_f:
                logger.warning(
                    "Signal rejected: SELL SL {:.2f} <= entry {:.2f} "
                    "(inverted stop) from '{}'",
                    sl_f,
                    entry_f,
                    candidate.strategy_name,
                )
                continue

            # --- Filter 1: R:R threshold (SIG-03) ---
            rr = float(candidate.risk_reward)
            if rr < trade_settings.min_risk_reward:
                logger.info(
                    "Signal rejected: R:R {:.2f} below minimum {:.2f}",
                    rr,
                    trade_settings.min_risk_reward,
                )
                continue

            # --- Filter 2: Max SL distance ---
            sl_dist = abs(float(candidate.entry_price) - float(candidate.stop_loss))
            sl_pips = sl_dist / _get_pip_value(candidate.symbol)
            if sl_pips > trade_settings.max_sl_pips:
                logger.info(
                    "Signal rejected: SL {:.0f} pips exceeds max {:.0f} pips",
                    sl_pips,
                    trade_settings.max_sl_pips,
                )
                continue

            # --- Filter 3: Confidence threshold (SIG-04) ---
            conf = float(candidate.confidence)
            if conf < trade_settings.min_confidence:
                logger.info(
                    "Signal rejected: confidence {:.1f}% below minimum {:.1f}%",
                    conf,
                    trade_settings.min_confidence,
                )
                continue

            # --- Filter 3: Dedup (SIG-05) ---
            dup_reason = await self._is_duplicate(session, candidate)
            if dup_reason is not None:
                logger.info(
                    "Signal suppressed: duplicate {} {} @ {} -- {}",
                    candidate.direction.value,
                    candidate.symbol,
                    candidate.entry_price,
                    dup_reason,
                )
                continue

            # --- Filter 3b: Per-strategy daily cap ---
            daily_limit_reason = await self._check_daily_strategy_limit(
                session, candidate
            )
            if daily_limit_reason is not None:
                logger.info(
                    "Signal suppressed: daily limit for '{}' -- {}",
                    candidate.strategy_name,
                    daily_limit_reason,
                )
                continue

            # --- Filter 3c: Per-strategy consecutive-loss cooldown ---
            cooldown_reason = await self._check_strategy_cooldown(session, candidate)
            if cooldown_reason is not None:
                logger.info(
                    "Signal suppressed: strategy cooldown for '{}' -- {}",
                    candidate.strategy_name,
                    cooldown_reason,
                )
                continue

            # --- Filter 4: Directional bias (SIG-07) ---
            if await self._check_directional_bias(session, candidate):
                logger.warning(
                    "Directional bias detected: >{}% of recent signals are {}",
                    int(BIAS_SKEW_THRESHOLD * 100),
                    candidate.direction.value,
                )
                # Informational only -- do NOT reject; append note to reasoning
                candidate = candidate.model_copy(
                    update={
                        "reasoning": (
                            candidate.reasoning
                            + " [NOTE: directional bias detected"
                            f" -- >{int(BIAS_SKEW_THRESHOLD * 100)}% of"
                            f" last {BIAS_WINDOW_SIGNALS} signals are"
                            f" {candidate.direction.value}]"
                        ),
                    }
                )

            validated.append(candidate)

        logger.info(
            "Validation complete: {}/{} candidates passed all filters",
            len(validated),
            len(candidates),
        )
        return validated

    async def _is_duplicate(
        self,
        session: AsyncSession,
        candidate: object,
    ) -> str | None:
        """Check if a candidate is a duplicate of an existing signal.

        Two checks run in order:

        1. **Time-window dedup** -- a same-direction active signal created
           within the last `DEDUP_WINDOW_HOURS` is treated as a burst
           duplicate (catches back-to-back scheduler ticks even at
           different price levels).
        2. **Price-distance dedup** -- a same-direction active signal
           whose entry price is within
           `trade_settings.dedup_price_distance_pips * PIP_VALUE` of the
           candidate's entry is treated as a positional duplicate. This
           catches the common case where strategies re-fire on the same
           recent candle across consecutive scheduler ticks; the entry
           prices end up identical (or near-identical) and would
           otherwise produce two parallel positions on the same setup.

        The price-distance check is independent of time -- it stays in
        force for as long as the existing signal is `active`.

        Args:
            session: Async database session.
            candidate: CandidateSignal with symbol, direction, and
                entry_price attributes.

        Returns:
            A short reason string if the candidate is a duplicate, or
            `None` if the candidate is unique.
        """
        # 1. Time-window dedup.
        #
        # Covers ALL resolved statuses (sl_hit, tp_hit, expired) in addition to
        # active signals so that a strategy cannot immediately re-enter the same
        # direction after a stop-out. Without this, the outcome detector (90s poll)
        # closes signal N before the next scanner tick, making dedup invisible to N+1.
        cutoff = datetime.now(timezone.utc) - timedelta(hours=DEDUP_WINDOW_HOURS)
        stmt = (
            select(Signal.id)
            .where(
                and_(
                    Signal.symbol == candidate.symbol,
                    Signal.direction == candidate.direction.value,
                    Signal.status.in_(list(DEDUP_STATUSES)),
                    Signal.created_at >= cutoff,
                )
            )
            .limit(1)
        )
        if (await session.execute(stmt)).scalar_one_or_none() is not None:
            return f"same direction within {DEDUP_WINDOW_HOURS}h window (any status)"

        # 2. Price-distance dedup against any active same-direction signal.
        trade_settings = await get_trade_settings(session)
        distance_pips = float(trade_settings.dedup_price_distance_pips)
        if distance_pips <= 0:
            return None

        # Keep the threshold as Decimal so the SQL comparison stays in
        # numeric domain end-to-end (Signal.entry_price is Numeric(10,2)).
        from decimal import Decimal as _Decimal

        max_price_distance = _Decimal(str(round(distance_pips * _get_pip_value(candidate.symbol), 2)))
        stmt2 = (
            select(Signal.entry_price)
            .where(
                and_(
                    Signal.symbol == candidate.symbol,
                    Signal.direction == candidate.direction.value,
                    Signal.status == "active",
                    func.abs(Signal.entry_price - candidate.entry_price)
                    <= max_price_distance,
                )
            )
            .limit(1)
        )
        nearest = (await session.execute(stmt2)).scalar_one_or_none()
        if nearest is not None:
            delta_pips = abs(
                float(candidate.entry_price) - float(nearest)
            ) / _get_pip_value(candidate.symbol)
            return (
                f"active same-direction signal at {nearest} "
                f"({delta_pips:.1f} pips away, threshold {distance_pips:.1f})"
            )

        return None

    async def _check_daily_strategy_limit(
        self,
        session: AsyncSession,
        candidate: object,
    ) -> str | None:
        """Enforce a per-strategy daily signal cap.

        Counts all signals (any status) generated today by the same strategy.
        If the count has already reached MAX_SIGNALS_PER_STRATEGY_DAY, the
        candidate is rejected to prevent a single strategy from flooding
        the signal feed on a single calendar day.

        Args:
            session: Async database session.
            candidate: CandidateSignal with strategy_name attribute.

        Returns:
            A short reason string if the cap is reached, or None if under limit.
        """
        from app.models.strategy import Strategy

        today_midnight = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        stmt = (
            select(func.count())
            .select_from(Signal)
            .join(Strategy, Strategy.id == Signal.strategy_id)
            .where(
                and_(
                    Strategy.name == candidate.strategy_name,
                    Signal.created_at >= today_midnight,
                )
            )
        )
        count = (await session.execute(stmt)).scalar_one()

        if count >= MAX_SIGNALS_PER_STRATEGY_DAY:
            return (
                f"{count} signals already generated today "
                f"(limit={MAX_SIGNALS_PER_STRATEGY_DAY})"
            )
        return None

    async def _check_strategy_cooldown(
        self,
        session: AsyncSession,
        candidate: object,
    ) -> str | None:
        """Block a strategy that has hit consecutive stop-losses recently.

        If the last STRATEGY_LOSS_STREAK_LIMIT outcomes for this strategy are
        all sl_hit AND the most recent one occurred within STRATEGY_COOLDOWN_HOURS,
        the strategy is placed in a temporary cool-down to prevent it from
        repeatedly re-entering a losing market condition.

        Args:
            session: Async database session.
            candidate: CandidateSignal with strategy_name attribute.

        Returns:
            A short reason string if in cooldown, or None if clear.
        """
        from app.models.outcome import Outcome
        from app.models.strategy import Strategy

        stmt = (
            select(Outcome.result, Outcome.created_at)
            .join(Signal, Signal.id == Outcome.signal_id)
            .join(Strategy, Strategy.id == Signal.strategy_id)
            .where(Strategy.name == candidate.strategy_name)
            .order_by(Outcome.created_at.desc())
            .limit(STRATEGY_LOSS_STREAK_LIMIT)
        )
        rows = (await session.execute(stmt)).all()

        if len(rows) < STRATEGY_LOSS_STREAK_LIMIT:
            return None  # Not enough history to judge

        all_sl = all(r[0] == "sl_hit" for r in rows)
        if not all_sl:
            return None  # Streak broken by a win or expiry

        most_recent = rows[0][1]
        if most_recent is None:
            return None

        if most_recent.tzinfo is None:
            most_recent = most_recent.replace(tzinfo=timezone.utc)

        hours_since = (
            datetime.now(timezone.utc) - most_recent
        ).total_seconds() / 3600

        if hours_since < STRATEGY_COOLDOWN_HOURS:
            remaining = round(STRATEGY_COOLDOWN_HOURS - hours_since, 1)
            return (
                f"{STRATEGY_LOSS_STREAK_LIMIT} consecutive SL hits; "
                f"cooldown active for {remaining}h more"
            )
        return None

    async def _check_directional_bias(
        self,
        session: AsyncSession,
        candidate: object,
    ) -> bool:
        """Detect if recent signal distribution is systematically skewed.

        Checks the last BIAS_WINDOW_SIGNALS signals. If the candidate's
        direction accounts for more than BIAS_SKEW_THRESHOLD of those
        signals, returns True (biased).

        Args:
            session: Async database session.
            candidate: CandidateSignal with direction attribute.

        Returns:
            True if directional bias is detected.
        """
        stmt = (
            select(Signal.direction)
            .order_by(Signal.created_at.desc())
            .limit(BIAS_WINDOW_SIGNALS)
        )
        result = await session.execute(stmt)
        directions = result.scalars().all()

        # Not enough data to judge bias
        if len(directions) < BIAS_WINDOW_SIGNALS:
            return False

        same_direction_count = sum(
            1 for d in directions if d == candidate.direction.value
        )
        ratio = same_direction_count / len(directions)

        return ratio > BIAS_SKEW_THRESHOLD

    def compute_expiry(self, candidate: object) -> datetime:
        """Compute the expiry timestamp for a candidate signal.

        Uses the timeframe-specific EXPIRY_HOURS mapping, defaulting
        to 8 hours if the timeframe is not explicitly configured.

        Expiry is based on the current wall-clock time (when the signal
        is persisted), NOT the candle timestamp — the candle may be
        hours old by the time the pipeline runs.

        Args:
            candidate: CandidateSignal with timeframe attribute.

        Returns:
            Expiry datetime (UTC).
        """
        expiry_hours = EXPIRY_HOURS.get(candidate.timeframe, 8)
        return datetime.now(timezone.utc) + timedelta(hours=expiry_hours)

    async def expire_stale_signals(self, session: AsyncSession) -> int:
        """Mark active signals past their expiry as expired.

        Runs before each scanner cycle to clean up stale signals.

        Args:
            session: Async database session.

        Returns:
            Number of signals expired.
        """
        now = datetime.now(timezone.utc)

        stmt = (
            update(Signal)
            .where(
                and_(
                    Signal.status == "active",
                    Signal.expires_at.isnot(None),
                    Signal.expires_at < now,
                )
            )
            .values(status="expired")
        )
        result = await session.execute(stmt)
        count = result.rowcount

        if count > 0:
            logger.info("Expired {} stale signal(s)", count)
        else:
            logger.debug("No stale signals to expire")

        return count
