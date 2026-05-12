"""Internal paper trading broker.

Opens trades from signals at current market price, monitors open positions
every few minutes against live price, and closes them at TP1 (50% + move SL
to breakeven), TP2, or SL.

Position sizing:
    units = risk_usd / |entry_price - stop_loss|

P&L:
    BUY:  pnl = (close_price - entry_price) * units
    SELL: pnl = (entry_price - close_price) * units
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal, ROUND_DOWN

import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_trade import PaperAccount, PaperTrade
from app.models.signal import Signal

_PAPER_BALANCE_ID = 1
_STARTING_BALANCE = Decimal("1000.00")


# ---------------------------------------------------------------------------
# Account helpers
# ---------------------------------------------------------------------------

async def get_or_create_account(session: AsyncSession) -> PaperAccount:
    result = await session.execute(
        select(PaperAccount).where(PaperAccount.id == _PAPER_BALANCE_ID)
    )
    account = result.scalar_one_or_none()
    if account is None:
        account = PaperAccount(
            id=_PAPER_BALANCE_ID,
            starting_balance=_STARTING_BALANCE,
            balance=_STARTING_BALANCE,
        )
        session.add(account)
        await session.flush()
    return account


async def get_account_summary(session: AsyncSession) -> dict:
    """Return account balance, equity, unrealised P&L, win stats."""
    account = await get_or_create_account(session)

    open_trades_result = await session.execute(
        select(PaperTrade).where(PaperTrade.status == "open")
    )
    open_trades = open_trades_result.scalars().all()

    closed_result = await session.execute(
        select(PaperTrade).where(PaperTrade.status == "closed")
    )
    closed_trades = closed_result.scalars().all()

    # Wins = trades that closed on tp1/tp2 (any TP hit = win)
    wins = sum(
        1 for t in closed_trades
        if t.close_reason in ("tp1", "tp2") or t.tp1_hit
    )
    losses = sum(1 for t in closed_trades if t.close_reason == "sl")
    breakevens = sum(1 for t in closed_trades if t.close_reason == "be")
    total_closed = len(closed_trades)
    win_rate = round(wins / total_closed * 100, 1) if total_closed else 0.0

    total_pnl = sum(float(t.realized_pnl_usd) for t in closed_trades)

    return {
        "balance": float(account.balance),
        "starting_balance": float(account.starting_balance),
        "unrealised_pnl": 0.0,  # updated by caller with live price
        "equity": float(account.balance),
        "total_pnl": round(total_pnl, 2),
        "win_rate": win_rate,
        "wins": wins,
        "losses": losses,
        "breakevens": breakevens,
        "total_trades": total_closed,
        "open_count": len(open_trades),
    }


# ---------------------------------------------------------------------------
# Open a trade from a signal
# ---------------------------------------------------------------------------

async def open_trade_from_signal(
    session: AsyncSession,
    signal: Signal,
    risk_per_trade_pct: float = 0.01,
) -> PaperTrade | None:
    """Create a PaperTrade from a persisted Signal.

    Skips if a trade for this signal already exists.
    """
    existing = await session.execute(
        select(PaperTrade).where(PaperTrade.signal_id == signal.id)
    )
    if existing.scalar_one_or_none():
        return None

    account = await get_or_create_account(session)
    balance = account.balance
    risk_usd = Decimal(str(risk_per_trade_pct)) * balance

    entry = signal.entry_price
    sl = signal.stop_loss
    sl_distance = abs(entry - sl)

    if sl_distance == 0:
        logger.warning("paper_broker: zero SL distance for signal {}, skipping", signal.id)
        return None

    units = (risk_usd / sl_distance).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    if units <= 0:
        return None

    trade = PaperTrade(
        signal_id=signal.id,
        symbol=signal.symbol,
        direction=signal.direction,
        initial_units=units,
        current_units=units,
        risk_amount_usd=risk_usd.quantize(Decimal("0.01")),
        entry_price=entry,
        stop_loss=sl,
        take_profit_1=signal.take_profit_1,
        take_profit_2=signal.take_profit_2,
        status="open",
        realized_pnl_usd=Decimal("0.00"),
    )
    session.add(trade)
    await session.flush()
    logger.info(
        "paper_broker: opened trade {} {} {} @ {} units={} risk=${}",
        trade.id, signal.direction, signal.symbol,
        float(entry), float(units), float(risk_usd),
    )
    return trade


# ---------------------------------------------------------------------------
# Check open positions against live price
# ---------------------------------------------------------------------------

def _calc_pnl(direction: str, entry: Decimal, price: Decimal, units: Decimal) -> Decimal:
    if direction == "BUY":
        return ((price - entry) * units).quantize(Decimal("0.01"))
    return ((entry - price) * units).quantize(Decimal("0.01"))


async def check_open_positions(session: AsyncSession, live_price: Decimal) -> None:
    """Evaluate all open paper trades against live_price and close/partial-close as needed."""
    result = await session.execute(
        select(PaperTrade).where(PaperTrade.status == "open")
    )
    trades = result.scalars().all()
    if not trades:
        return

    account = await get_or_create_account(session)
    now = datetime.now(UTC)

    for trade in trades:
        direction = trade.direction
        price = live_price
        entry = trade.entry_price
        sl = trade.stop_loss
        tp1 = trade.take_profit_1
        tp2 = trade.take_profit_2
        units = trade.current_units

        hit_tp2 = (price >= tp2) if direction == "BUY" else (price <= tp2)
        hit_tp1 = (price >= tp1) if direction == "BUY" else (price <= tp1)
        hit_sl  = (price <= sl)  if direction == "BUY" else (price >= sl)

        if not trade.tp1_hit and hit_tp1 and not hit_tp2:
            # Partial close 50% at TP1, move SL to breakeven
            half = (units / 2).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
            tp1_pnl = _calc_pnl(direction, entry, tp1, half)

            trade.tp1_hit = True
            trade.tp1_price = tp1
            trade.tp1_pnl_usd = tp1_pnl
            trade.tp1_hit_at = now
            trade.current_units = units - half
            trade.stop_loss = entry  # move to breakeven
            trade.realized_pnl_usd = tp1_pnl

            account.balance = (account.balance + tp1_pnl).quantize(Decimal("0.01"))
            logger.info(
                "paper_broker: trade {} TP1 hit @ {} pnl=${} (50% closed, SL→BE)",
                trade.id, float(tp1), float(tp1_pnl),
            )

        elif hit_tp2:
            # Close remaining position at TP2
            final_pnl = _calc_pnl(direction, entry, tp2, trade.current_units)
            total_pnl = (trade.tp1_pnl_usd or Decimal("0")) + final_pnl

            trade.close_price = tp2
            trade.close_reason = "tp2"
            trade.closed_at = now
            trade.status = "closed"
            trade.realized_pnl_usd = total_pnl
            account.balance = (account.balance + final_pnl).quantize(Decimal("0.01"))
            logger.info(
                "paper_broker: trade {} closed TP2 @ {} total_pnl=${}",
                trade.id, float(tp2), float(total_pnl),
            )

        elif hit_sl:
            # Stop loss hit
            final_pnl = _calc_pnl(direction, entry, sl, trade.current_units)
            reason = "be" if trade.tp1_hit else "sl"
            total_pnl = (trade.tp1_pnl_usd or Decimal("0")) + final_pnl

            trade.close_price = sl
            trade.close_reason = reason
            trade.closed_at = now
            trade.status = "closed"
            trade.realized_pnl_usd = total_pnl

            if not trade.tp1_hit:
                account.balance = (account.balance + final_pnl).quantize(Decimal("0.01"))
            # If TP1 already hit and SL is at breakeven, final_pnl ≈ 0, no balance change needed

            logger.info(
                "paper_broker: trade {} closed {} @ {} pnl=${}",
                trade.id, reason.upper(), float(sl), float(total_pnl),
            )

    await session.commit()


# ---------------------------------------------------------------------------
# Fetch live price for a symbol from Twelve Data
# ---------------------------------------------------------------------------

async def fetch_live_price(symbol: str, api_key: str) -> Decimal | None:
    from app.market import to_twelve_data_symbol
    td_symbol = to_twelve_data_symbol(symbol)
    url = f"https://api.twelvedata.com/price?symbol={td_symbol}&apikey={api_key}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            data = resp.json()
            price_str = data.get("price")
            if price_str:
                return Decimal(str(price_str))
    except Exception as exc:
        logger.warning("paper_broker: failed to fetch live price for {}: {}", symbol, exc)
    return None


# ---------------------------------------------------------------------------
# Get open trades formatted for dashboard
# ---------------------------------------------------------------------------

async def get_open_trades(session: AsyncSession, live_price: Decimal | None = None) -> list[dict]:
    result = await session.execute(
        select(PaperTrade).where(PaperTrade.status == "open").order_by(PaperTrade.opened_at.desc())
    )
    trades = result.scalars().all()
    out = []
    for t in trades:
        unrealised = None
        if live_price is not None:
            unrealised = float(_calc_pnl(t.direction, t.entry_price, live_price, t.current_units))
        out.append({
            "id": t.id,
            "symbol": t.symbol,
            "direction": t.direction,
            "entry_price": float(t.entry_price),
            "stop_loss": float(t.stop_loss),
            "take_profit_1": float(t.take_profit_1),
            "take_profit_2": float(t.take_profit_2),
            "units": float(t.current_units),
            "risk_usd": float(t.risk_amount_usd),
            "tp1_hit": t.tp1_hit,
            "unrealised_pnl": unrealised,
            "opened_at": t.opened_at.isoformat() if t.opened_at else None,
        })
    return out


async def get_trade_history(session: AsyncSession, limit: int = 50) -> list[dict]:
    result = await session.execute(
        select(PaperTrade)
        .where(PaperTrade.status == "closed")
        .order_by(PaperTrade.closed_at.desc())
        .limit(limit)
    )
    trades = result.scalars().all()
    out = []
    for t in trades:
        out.append({
            "id": t.id,
            "symbol": t.symbol,
            "direction": t.direction,
            "entry_price": float(t.entry_price),
            "close_price": float(t.close_price) if t.close_price else None,
            "close_reason": t.close_reason,
            "tp1_hit": t.tp1_hit,
            "realized_pnl_usd": float(t.realized_pnl_usd),
            "opened_at": t.opened_at.isoformat() if t.opened_at else None,
            "closed_at": t.closed_at.isoformat() if t.closed_at else None,
        })
    return out
