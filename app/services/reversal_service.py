"""Stop-and-Reverse (SAR) service for paper trading.

Logic
-----
Every time check_paper_trades runs (every 2 min) this service evaluates every
open paper trade:

1. If price has moved adversely past REVERSAL_THRESHOLD_PCT of the SL distance,
   a trend-confirmation check runs using EMA-9 vs EMA-21 on the last 30 H1
   candles.

2. If trend is confirmed in the reversal direction, the original trade is closed
   at the live price (close_reason="reversed") and a new paper trade opens in
   the opposite direction, reusing the same SL/TP pip distances from the entry.

3. A ReversalLog row is written so the trade is never reversed twice.

Trades where TP1 has already been hit are skipped — the SL is already at
breakeven so there is no meaningful adverse risk to protect against.

Constants
---------
REVERSAL_THRESHOLD_PCT : float
    Fraction of SL distance that must be breached before evaluation (default
    0.60 = 60 %).  e.g. entry=4699, SL=4739 (+40 pts), threshold at +24 pts.
H1_CANDLES_NEEDED : int
    Minimum H1 candles required for EMA-21 warm-up (default 25).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_trade import PaperAccount, PaperTrade
from app.models.reversal_log import ReversalLog
from app.models.candle import Candle
from app.services.paper_broker import _calc_pnl, get_or_create_account
from app.strategies.helpers.indicators import compute_ema
from app.strategies.base import candles_to_dataframe

REVERSAL_THRESHOLD_PCT = Decimal("0.60")
H1_CANDLES_NEEDED = 25


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def check_and_reverse_positions(session: AsyncSession, live_price: Decimal) -> None:
    """Evaluate open paper trades for stop-and-reverse candidates.

    Called from check_paper_trades in jobs.py after the normal TP/SL check.
    """
    now = datetime.now(UTC)

    result = await session.execute(
        select(PaperTrade).where(PaperTrade.status == "open")
    )
    open_trades = result.scalars().all()
    if not open_trades:
        return

    # IDs that have already triggered a reversal — skip them
    reversed_result = await session.execute(
        select(ReversalLog.original_trade_id)
    )
    already_reversed: set[int] = {row[0] for row in reversed_result}

    account = await get_or_create_account(session)

    for trade in open_trades:
        if trade.id in already_reversed:
            continue

        # Skip trades where TP1 is hit — SL is at breakeven, no real risk
        if trade.tp1_hit:
            continue

        sl_dist = abs(trade.entry_price - trade.stop_loss)
        if sl_dist == 0:
            continue

        threshold_dist = sl_dist * REVERSAL_THRESHOLD_PCT

        # Check whether live price has breached the adverse threshold
        if trade.direction == "SELL":
            # Adverse move for a SELL = price rising above entry
            threshold_price = trade.entry_price + threshold_dist
            crossed = live_price >= threshold_price
        else:
            # Adverse move for a BUY = price falling below entry
            threshold_price = trade.entry_price - threshold_dist
            crossed = live_price <= threshold_price

        if not crossed:
            continue

        logger.info(
            "reversal_service: trade {} {} crossed {:.0f}% threshold "
            "(entry={} threshold={} live={})",
            trade.id,
            trade.direction,
            float(REVERSAL_THRESHOLD_PCT * 100),
            float(trade.entry_price),
            float(threshold_price),
            float(live_price),
        )

        # Trend confirmation — must agree with reversal direction
        confirmed = await _confirm_trend(session, trade.direction, live_price, trade.symbol)
        if not confirmed:
            logger.info(
                "reversal_service: trade {} threshold crossed but trend not "
                "confirmed — will retry next cycle",
                trade.id,
            )
            continue

        await _execute_reversal(session, trade, live_price, account, now)


# ---------------------------------------------------------------------------
# Trend confirmation
# ---------------------------------------------------------------------------

async def _confirm_trend(
    session: AsyncSession,
    original_direction: str,
    live_price: Decimal,
    symbol: str,
) -> bool:
    """Return True if H1 EMA-9/21 confirms a reversal against original_direction.

    BUY reversal (original was SELL, price rising):
        EMA-9 > EMA-21  AND  price > EMA-21

    SELL reversal (original was BUY, price falling):
        EMA-9 < EMA-21  AND  price < EMA-21
    """
    result = await session.execute(
        select(Candle)
        .where(Candle.symbol == symbol, Candle.timeframe == "H1")
        .order_by(Candle.timestamp.desc())
        .limit(30)
    )
    candles = result.scalars().all()

    if len(candles) < H1_CANDLES_NEEDED:
        logger.warning(
            "reversal_service: only {} H1 candles available (need {}), "
            "skipping trend confirmation for {}",
            len(candles), H1_CANDLES_NEEDED, symbol,
        )
        return False

    df = candles_to_dataframe(candles)
    closes = df["close"]

    ema_9 = compute_ema(closes, 9)
    ema_21 = compute_ema(closes, 21)

    last_ema9 = ema_9.iloc[-1]
    last_ema21 = ema_21.iloc[-1]

    if pd.isna(last_ema9) or pd.isna(last_ema21):
        logger.warning("reversal_service: EMA returned NaN, skipping confirmation")
        return False

    price_f = float(live_price)

    if original_direction == "SELL":
        # SELL gone wrong → confirm BUY reversal: short MA above long MA, price above MA
        confirmed = last_ema9 > last_ema21 and price_f > last_ema21
    else:
        # BUY gone wrong → confirm SELL reversal: short MA below long MA, price below MA
        confirmed = last_ema9 < last_ema21 and price_f < last_ema21

    logger.debug(
        "reversal_service: trend check for {} — ema9={:.2f} ema21={:.2f} "
        "price={:.2f} confirmed={}",
        original_direction, last_ema9, last_ema21, price_f, confirmed,
    )
    return confirmed


# ---------------------------------------------------------------------------
# Execute the reversal
# ---------------------------------------------------------------------------

async def _execute_reversal(
    session: AsyncSession,
    trade: PaperTrade,
    live_price: Decimal,
    account: PaperAccount,
    now: datetime,
) -> None:
    """Close original trade early and open a new trade in the opposite direction.

    The reversal trade mirrors the original signal's SL/TP pip distances,
    anchored to the current live price as the new entry.
    """
    # 1. Close original trade at live price
    final_pnl = _calc_pnl(
        trade.direction, trade.entry_price, live_price, trade.current_units
    )
    total_pnl = (trade.tp1_pnl_usd or Decimal("0")) + final_pnl

    trade.close_price = live_price
    trade.close_reason = "reversed"
    trade.closed_at = now
    trade.status = "closed"
    trade.realized_pnl_usd = total_pnl
    account.balance = (account.balance + final_pnl).quantize(Decimal("0.01"))

    logger.info(
        "reversal_service: closed trade {} {} @ {} pnl=${:.2f} (early reversal)",
        trade.id, trade.direction, float(live_price), float(total_pnl),
    )

    # 2. Build the reversal trade — mirror original SL/TP distances from new entry
    new_direction = "BUY" if trade.direction == "SELL" else "SELL"
    entry = live_price

    sl_dist = abs(trade.entry_price - trade.stop_loss)
    tp1_dist = abs(trade.entry_price - trade.take_profit_1)
    tp2_dist = abs(trade.entry_price - trade.take_profit_2)

    if new_direction == "BUY":
        new_sl = entry - sl_dist
        new_tp1 = entry + tp1_dist
        new_tp2 = entry + tp2_dist
    else:
        new_sl = entry + sl_dist
        new_tp1 = entry - tp1_dist
        new_tp2 = entry - tp2_dist

    reversal_trade = PaperTrade(
        signal_id=None,          # not from signal scanner
        symbol=trade.symbol,
        direction=new_direction,
        initial_units=trade.initial_units,   # same contract size
        current_units=trade.initial_units,
        risk_amount_usd=trade.risk_amount_usd,
        entry_price=entry,
        stop_loss=new_sl,
        take_profit_1=new_tp1,
        take_profit_2=new_tp2,
        status="open",
        realized_pnl_usd=Decimal("0.00"),
    )
    session.add(reversal_trade)
    await session.flush()  # populate reversal_trade.id

    # 3. Write reversal log (prevents re-triggering on same original trade)
    log = ReversalLog(
        original_trade_id=trade.id,
        reversal_trade_id=reversal_trade.id,
        trigger_price=live_price,
        threshold_pct=REVERSAL_THRESHOLD_PCT * 100,
        trend_confirmed=True,
        reversed_at=now,
    )
    session.add(log)
    await session.commit()

    logger.info(
        "reversal_service: opened reversal trade {} {} @ {} "
        "sl={} tp1={} tp2={}",
        reversal_trade.id, new_direction, float(entry),
        float(new_sl), float(new_tp1), float(new_tp2),
    )
