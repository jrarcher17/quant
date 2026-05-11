"""Order execution and position sync services.

OrderExecutor  — called after the signal pipeline runs; places orders for
                 each new signal and persists a BrokerOrder row.

PositionSyncer — called when a signal is closed (TP/SL hit or expired);
                 closes the corresponding OANDA trade if one is open.
"""

import math
from decimal import Decimal
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.broker_order import BrokerOrder
from app.models.signal import Signal
from app.services.broker.base import BrokerAdapter
from app.services.broker.oanda import OandaAdapter, symbol_to_oanda
from app.services.trade_settings import get_trade_settings


def _build_adapter() -> Optional[BrokerAdapter]:
    """Return a configured BrokerAdapter, or None if integration is disabled."""
    settings = get_settings()
    if not settings.oanda_enabled:
        return None
    if not settings.oanda_api_token or not settings.oanda_account_id:
        logger.warning(
            "OANDA integration enabled but OANDA_API_TOKEN / OANDA_ACCOUNT_ID are not set."
        )
        return None
    return OandaAdapter(
        api_token=settings.oanda_api_token,
        account_id=settings.oanda_account_id,
        mode=settings.oanda_mode,
    )


def _compute_units(
    direction: str,
    entry_price: Decimal,
    stop_loss: Decimal,
    account_balance: Decimal,
    risk_per_trade_pct: Decimal,
) -> Decimal:
    """Compute the position size in broker units (oz for XAU_USD).

    Formula:
        sl_distance = abs(entry_price - stop_loss)   [in price currency]
        risk_usd    = account_balance * risk_pct
        units       = floor(risk_usd / sl_distance)
        units       = max(units, 1)

    For XAUUSD: 1 unit = 1 troy oz. P&L per unit = price movement in USD.
    """
    sl_distance = abs(entry_price - stop_loss)
    if sl_distance <= 0:
        return Decimal("1")

    risk_usd = account_balance * risk_per_trade_pct
    raw_units = float(risk_usd) / float(sl_distance)
    units = max(math.floor(raw_units), 1)

    if direction == "SELL":
        return Decimal(str(-units))
    return Decimal(str(units))


class OrderExecutor:
    """Place broker orders for freshly generated signals."""

    async def execute_signals(
        self,
        session: AsyncSession,
        signals: list[Signal],
    ) -> None:
        """Place an order for each signal in the list.

        Errors are caught per-signal so a single rejection does not prevent
        the remaining signals from being submitted.
        """
        if not signals:
            return

        adapter = _build_adapter()
        if adapter is None:
            logger.debug("OrderExecutor: broker integration disabled, skipping order placement.")
            return

        settings = get_settings()
        trade_settings = await get_trade_settings(session)

        # Fetch live account balance for position sizing
        try:
            acct = await adapter.get_account()
            account_balance = acct.balance
        except Exception as exc:
            logger.error("OrderExecutor: could not fetch account balance | error={}", exc)
            return

        for signal in signals:
            await self._place_one(
                session=session,
                adapter=adapter,
                signal=signal,
                account_balance=account_balance,
                risk_per_trade_pct=Decimal(str(trade_settings.risk_per_trade_pct)),
                mode=settings.oanda_mode,
            )

    async def _place_one(
        self,
        session: AsyncSession,
        adapter: BrokerAdapter,
        signal: Signal,
        account_balance: Decimal,
        risk_per_trade_pct: Decimal,
        mode: str,
    ) -> None:
        instrument = symbol_to_oanda(signal.symbol)
        units = _compute_units(
            direction=signal.direction,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            account_balance=account_balance,
            risk_per_trade_pct=risk_per_trade_pct,
        )

        # Use TP1 as the broker-managed take profit; TP2 is tracked by our
        # outcome detector and notified separately.
        result = await adapter.place_market_order(
            instrument=instrument,
            units=units,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit_1,
            client_order_id=f"apexq-signal-{signal.id}",
        )

        broker_order = BrokerOrder(
            signal_id=signal.id,
            broker_order_id=result.order_id,
            broker_trade_id=result.trade_id,
            instrument=instrument,
            units=units,
            fill_price=result.fill_price,
            mode=mode,
            status="filled" if result.success and result.trade_id else (
                "error" if not result.success else "pending"
            ),
            error_detail=result.error,
        )
        session.add(broker_order)
        await session.commit()

        if result.success:
            logger.info(
                "OrderExecutor: order placed | signal_id={} instrument={} units={} "
                "trade_id={} fill={}",
                signal.id,
                instrument,
                units,
                result.trade_id,
                result.fill_price,
            )
        else:
            logger.warning(
                "OrderExecutor: order rejected | signal_id={} instrument={} error={}",
                signal.id,
                instrument,
                result.error,
            )


class PositionSyncer:
    """Close OANDA trades when the corresponding signal closes."""

    async def sync_closed(
        self,
        session: AsyncSession,
        signal: Signal,
    ) -> None:
        """If a filled BrokerOrder exists for this signal, close the trade.

        Called from check_outcomes after each outcome is detected. Errors are
        swallowed so outcome recording is never blocked by broker issues.
        """
        adapter = _build_adapter()
        if adapter is None:
            return

        try:
            stmt = (
                select(BrokerOrder)
                .where(
                    BrokerOrder.signal_id == signal.id,
                    BrokerOrder.status == "filled",
                    BrokerOrder.broker_trade_id.isnot(None),
                )
                .order_by(BrokerOrder.created_at.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            broker_order = result.scalar_one_or_none()

            if broker_order is None:
                return

            closed = await adapter.close_trade(broker_order.broker_trade_id)
            if closed:
                broker_order.status = "closed"
                await session.commit()
                logger.info(
                    "PositionSyncer: trade closed | signal_id={} trade_id={}",
                    signal.id,
                    broker_order.broker_trade_id,
                )
            else:
                logger.warning(
                    "PositionSyncer: could not close trade | signal_id={} trade_id={}",
                    signal.id,
                    broker_order.broker_trade_id,
                )

        except Exception as exc:
            logger.error(
                "PositionSyncer.sync_closed failed | signal_id={} error={}",
                signal.id,
                exc,
            )
