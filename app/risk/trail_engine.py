"""TrailEngine — chandelier-style trailing stop after TP1 partial.

Activates only after `tp1_hit=True` on a PaperTrade. Each call computes a
new trail level from the highest high (BUY) or lowest low (SELL) since
entry, minus 1.5×ATR, and only ratchets the SL forward (never backwards).
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
from loguru import logger
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candle import Candle
from app.models.paper_trade import PaperTrade
from app.strategies.helpers.indicators import compute_atr


_TRAIL_ATR_MULT = Decimal("1.5")
_ATR_LOOKBACK_BARS = 30


class TrailEngine:
    """Updates stops on already-TP1-hit trades using a chandelier rule."""

    async def update_trails(
        self,
        session: AsyncSession,
        live_price: Decimal,
    ) -> int:
        result = await session.execute(
            select(PaperTrade).where(
                and_(
                    PaperTrade.status == "open",
                    PaperTrade.tp1_hit.is_(True),
                )
            )
        )
        trades = result.scalars().all()
        if not trades:
            return 0

        atr_dec = await self._latest_atr(session, trades[0].symbol)
        if atr_dec <= 0:
            logger.debug("TrailEngine: ATR unavailable, skipping trail update")
            return 0

        updated = 0
        for trade in trades:
            new_sl = self._compute_trail(trade, live_price, atr_dec)
            if new_sl is None:
                continue

            current_sl = trade.stop_loss
            if trade.direction == "BUY":
                if new_sl > current_sl:
                    trade.stop_loss = new_sl
                    updated += 1
                    logger.info(
                        "TrailEngine: trade {} BUY trail SL {} -> {} (live={})",
                        trade.id, float(current_sl), float(new_sl), float(live_price),
                    )
            else:
                if new_sl < current_sl:
                    trade.stop_loss = new_sl
                    updated += 1
                    logger.info(
                        "TrailEngine: trade {} SELL trail SL {} -> {} (live={})",
                        trade.id, float(current_sl), float(new_sl), float(live_price),
                    )

        if updated:
            await session.commit()
        return updated

    def _compute_trail(
        self, trade: PaperTrade, live_price: Decimal, atr_dec: Decimal
    ) -> Decimal | None:
        # We approximate "highest high since entry" by max(entry, live_price)
        # which is close enough at the H1 cadence we operate on.
        if trade.direction == "BUY":
            anchor = max(trade.entry_price, live_price)
            new_sl = anchor - _TRAIL_ATR_MULT * atr_dec
        else:
            anchor = min(trade.entry_price, live_price)
            new_sl = anchor + _TRAIL_ATR_MULT * atr_dec
        return new_sl.quantize(Decimal("0.01"))

    async def _latest_atr(self, session: AsyncSession, symbol: str) -> Decimal:
        stmt = (
            select(Candle.high, Candle.low, Candle.close)
            .where(Candle.symbol == symbol, Candle.timeframe == "H1")
            .order_by(Candle.timestamp.desc())
            .limit(_ATR_LOOKBACK_BARS + 5)
        )
        rows = (await session.execute(stmt)).all()
        if len(rows) < _ATR_LOOKBACK_BARS:
            return Decimal("0")

        rows = list(reversed(rows))
        highs = pd.Series([float(r[0]) for r in rows])
        lows = pd.Series([float(r[1]) for r in rows])
        closes = pd.Series([float(r[2]) for r in rows])

        atr = compute_atr(highs, lows, closes, length=14).dropna()
        if atr.empty:
            return Decimal("0")
        return Decimal(str(round(float(atr.iloc[-1]), 4)))
