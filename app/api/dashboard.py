"""Dashboard API endpoint — matrix-style operational dashboard."""

import datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_session
from app.market import to_twelve_data_symbol
from app.models.backtest_result import BacktestResult
from app.models.candle import Candle
from app.models.optimized_params import OptimizedParams
from app.models.outcome import Outcome
from app.models.signal import Signal
from app.models.strategy import Strategy
from app.models.strategy_performance import StrategyPerformance
from app.services.trade_settings import (
    TradeSettingsPayload,
    get_trade_settings,
    update_trade_settings,
)
from app.workers.scheduler import scheduler

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

# Track app start time for uptime
_start_time: datetime.datetime = datetime.datetime.now(datetime.UTC)


@router.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    """Serve the dashboard HTML page."""
    settings = get_settings()
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "symbol": settings.trading_symbol,
        },
    )


@router.get("/data")
async def dashboard_data(
    session: AsyncSession = Depends(get_session),
):
    """Return all dashboard data as a single JSON payload."""
    symbol = get_settings().trading_symbol
    now = datetime.datetime.now(datetime.UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    uptime = (now - _start_time).total_seconds()

    # --- System status ---
    db_status = "connected"
    try:
        from sqlalchemy import text
        await session.execute(text("SELECT 1"))
    except Exception:
        db_status = "disconnected"

    scheduler_status = "running" if scheduler.running else "stopped"

    # --- Scheduler jobs ---
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            "trigger": str(job.trigger),
        })

    # --- Signal stats ---
    active_signals = 0
    signals_today = 0
    total_signals = 0
    try:
        result = await session.execute(
            select(func.count()).select_from(Signal).where(
                Signal.symbol == symbol,
                Signal.status == "active",
            )
        )
        active_signals = result.scalar_one()

        result = await session.execute(
            select(func.count()).select_from(Signal).where(
                Signal.symbol == symbol,
                Signal.created_at >= today_start
            )
        )
        signals_today = result.scalar_one()

        result = await session.execute(
            select(func.count()).select_from(Signal).where(Signal.symbol == symbol)
        )
        total_signals = result.scalar_one()
    except Exception:
        pass

    # --- Recent signals (last 20) ---
    recent_signals = []
    try:
        query = (
            select(Signal, Outcome, Strategy.name)
            .outerjoin(Outcome, Signal.id == Outcome.signal_id)
            .outerjoin(Strategy, Signal.strategy_id == Strategy.id)
            .where(Signal.symbol == symbol)
            .order_by(Signal.created_at.desc())
            .limit(20)
        )
        result = await session.execute(query)
        for signal, outcome, strategy_name in result.all():
            recent_signals.append({
                "id": signal.id,
                "direction": signal.direction,
                "entry": float(signal.entry_price),
                "sl": float(signal.stop_loss),
                "tp1": float(signal.take_profit_1),
                "tp2": float(signal.take_profit_2),
                "rr": float(signal.risk_reward),
                "confidence": float(signal.confidence),
                "status": signal.status,
                "strategy": strategy_name or "Unknown",
                "created": signal.created_at.isoformat() if signal.created_at else None,
                "result": outcome.result if outcome else None,
                "pnl": float(outcome.pnl_pips) if outcome else None,
            })
    except Exception:
        pass

    # --- Outcome stats ---
    wins = 0
    losses = 0
    total_pnl = 0.0
    try:
        result = await session.execute(
            select(
                func.count().filter(Outcome.result.in_(["tp1_hit", "tp2_hit"])).label("wins"),
                func.count().filter(Outcome.result == "sl_hit").label("losses"),
                func.coalesce(func.sum(Outcome.pnl_pips), 0).label("total_pnl"),
            ).select_from(Outcome)
        )
        row = result.one()
        wins = row.wins
        losses = row.losses
        total_pnl = float(row.total_pnl)
    except Exception:
        pass

    # --- Strategy performance ---
    strategies = []
    try:
        query = (
            select(Strategy.name, StrategyPerformance)
            .join(Strategy, StrategyPerformance.strategy_id == Strategy.id)
            .where(StrategyPerformance.period == "30d")
            .order_by(StrategyPerformance.win_rate.desc())
        )
        result = await session.execute(query)
        for name, perf in result.all():
            strategies.append({
                "name": name,
                "win_rate": float(perf.win_rate),
                "profit_factor": float(perf.profit_factor),
                "avg_rr": float(perf.avg_rr),
                "total_signals": perf.total_signals,
                "is_degraded": perf.is_degraded,
            })
    except Exception:
        pass

    # --- Last candle fetch ---
    last_candle = None
    current_price = None
    price_change = None
    price_change_pct = None
    try:
        latest_candle_query = (
            select(Candle)
            .where(Candle.symbol == symbol)
            .order_by(Candle.timestamp.desc())
            .limit(1)
        )
        result = await session.execute(latest_candle_query)
        latest_candle = result.scalar_one_or_none()
        if latest_candle:
            last_candle = latest_candle.timestamp.isoformat()
            current_price = float(latest_candle.close)

            previous_candle_query = (
                select(Candle)
                .where(
                    Candle.symbol == symbol,
                    Candle.timestamp < latest_candle.timestamp,
                )
                .order_by(Candle.timestamp.desc())
                .limit(1)
            )
            result = await session.execute(previous_candle_query)
            previous_candle = result.scalar_one_or_none()
            if previous_candle and previous_candle.close:
                previous_close = float(previous_candle.close)
                price_change = current_price - previous_close
                if previous_close != 0:
                    price_change_pct = (price_change / previous_close) * 100
    except Exception:
        pass

    # --- Backtest results (latest per strategy per window) ---
    backtests = []
    total_backtests = 0
    try:
        # Count total backtest results
        result = await session.execute(
            select(func.count()).select_from(BacktestResult).where(
                BacktestResult.symbol == symbol
            )
        )
        total_backtests = result.scalar_one()

        # Get latest backtest result per strategy per window_days (non-walk-forward)
        from sqlalchemy import and_

        # Subquery: max created_at per (strategy_id, window_days)
        latest_sub = (
            select(
                BacktestResult.strategy_id,
                BacktestResult.window_days,
                func.max(BacktestResult.created_at).label("max_created"),
            )
            .where(
                BacktestResult.symbol == symbol,
                BacktestResult.is_walk_forward.isnot(True),
            )
            .group_by(BacktestResult.strategy_id, BacktestResult.window_days)
            .subquery()
        )

        bt_query = (
            select(BacktestResult, Strategy.name)
            .join(Strategy, BacktestResult.strategy_id == Strategy.id)
            .where(BacktestResult.symbol == symbol)
            .join(
                latest_sub,
                and_(
                    BacktestResult.strategy_id == latest_sub.c.strategy_id,
                    BacktestResult.window_days == latest_sub.c.window_days,
                    BacktestResult.created_at == latest_sub.c.max_created,
                ),
            )
            .order_by(Strategy.name, BacktestResult.window_days)
        )
        result = await session.execute(bt_query)
        for bt, strat_name in result.all():
            backtests.append({
                "strategy": strat_name,
                "window_days": bt.window_days,
                "win_rate": float(bt.win_rate) if bt.win_rate is not None else None,
                "profit_factor": float(bt.profit_factor) if bt.profit_factor is not None else None,
                "sharpe_ratio": float(bt.sharpe_ratio) if bt.sharpe_ratio is not None else None,
                "max_drawdown": float(bt.max_drawdown) if bt.max_drawdown is not None else None,
                "expectancy": float(bt.expectancy) if bt.expectancy is not None else None,
                "total_trades": bt.total_trades,
                "is_walk_forward": bt.is_walk_forward or False,
                "is_overfitted": bt.is_overfitted,
                "created": bt.created_at.isoformat() if bt.created_at else None,
            })
    except Exception:
        pass

    # --- Walk-forward validation results (latest per strategy) ---
    walk_forward = []
    try:
        wf_latest_sub = (
            select(
                BacktestResult.strategy_id,
                func.max(BacktestResult.created_at).label("max_created"),
            )
            .where(
                BacktestResult.symbol == symbol,
                BacktestResult.is_walk_forward.is_(True),
            )
            .group_by(BacktestResult.strategy_id)
            .subquery()
        )
        wf_query = (
            select(BacktestResult, Strategy.name)
            .join(Strategy, BacktestResult.strategy_id == Strategy.id)
            .where(BacktestResult.symbol == symbol)
            .join(
                wf_latest_sub,
                and_(
                    BacktestResult.strategy_id == wf_latest_sub.c.strategy_id,
                    BacktestResult.created_at == wf_latest_sub.c.max_created,
                ),
            )
            .order_by(Strategy.name)
        )
        result = await session.execute(wf_query)
        for bt, strat_name in result.all():
            walk_forward.append({
                "strategy": strat_name,
                "win_rate": float(bt.win_rate) if bt.win_rate is not None else None,
                "profit_factor": float(bt.profit_factor) if bt.profit_factor is not None else None,
                "total_trades": bt.total_trades,
                "is_overfitted": bt.is_overfitted,
                "wfe": float(bt.walk_forward_efficiency) if bt.walk_forward_efficiency is not None else None,
                "created": bt.created_at.isoformat() if bt.created_at else None,
            })
    except Exception:
        pass

    # --- Optimized params (latest active per strategy) ---
    opt_params_list = []
    try:
        opt_latest_sub = (
            select(
                OptimizedParams.strategy_name,
                func.max(OptimizedParams.created_at).label("max_created"),
            )
            .where(OptimizedParams.is_active.is_(True))
            .group_by(OptimizedParams.strategy_name)
            .subquery()
        )
        opt_query = (
            select(OptimizedParams)
            .join(
                opt_latest_sub,
                and_(
                    OptimizedParams.strategy_name == opt_latest_sub.c.strategy_name,
                    OptimizedParams.created_at == opt_latest_sub.c.max_created,
                ),
            )
            .order_by(OptimizedParams.strategy_name)
        )
        result = await session.execute(opt_query)
        for opt in result.scalars().all():
            opt_params_list.append({
                "strategy": opt.strategy_name,
                "win_rate": float(opt.win_rate) if opt.win_rate is not None else None,
                "profit_factor": float(opt.profit_factor) if opt.profit_factor is not None else None,
                "total_trades": opt.total_trades,
                "wfe_ratio": float(opt.wfe_ratio) if opt.wfe_ratio is not None else None,
                "is_overfitted": opt.is_overfitted,
                "combinations_tested": opt.combinations_tested,
                "created": opt.created_at.isoformat() if opt.created_at else None,
            })
    except Exception:
        pass

    return {
        "system": {
            "status": "operational" if db_status == "connected" and scheduler_status == "running" else "degraded",
            "symbol": symbol,
            "current_price": current_price,
            "price_change": price_change,
            "price_change_pct": price_change_pct,
            "database": db_status,
            "scheduler": scheduler_status,
            "uptime_seconds": round(uptime, 1),
            "last_candle": last_candle,
            "timestamp": now.isoformat(),
        },
        "jobs": jobs,
        "signals": {
            "active": active_signals,
            "today": signals_today,
            "total": total_signals,
            "recent": recent_signals,
        },
        "performance": {
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0,
            "total_pnl": round(total_pnl, 2),
        },
        "strategies": strategies,
        "backtests": {
            "total": total_backtests,
            "results": backtests,
            "walk_forward": walk_forward,
            "optimized_params": opt_params_list,
        },
    }


@router.get("/settings")
async def dashboard_settings(
    session: AsyncSession = Depends(get_session),
) -> TradeSettingsPayload:
    """Return user-editable trade settings."""
    return await get_trade_settings(session)


@router.post("/settings")
async def save_dashboard_settings(
    payload: TradeSettingsPayload,
    session: AsyncSession = Depends(get_session),
) -> TradeSettingsPayload:
    """Persist user-editable trade settings."""
    return await update_trade_settings(session, payload)


@router.get("/price")
async def dashboard_price(
    session: AsyncSession = Depends(get_session),
):
    """Return the latest live quote for the configured symbol."""
    settings = get_settings()
    symbol = settings.trading_symbol
    now = datetime.datetime.now(datetime.UTC)
    price = None
    source = "live"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.twelvedata.com/price",
                params={
                    "symbol": to_twelve_data_symbol(symbol),
                    "apikey": settings.twelve_data_api_key,
                },
            )
            response.raise_for_status()
            data = response.json()
            if "price" in data:
                price = float(data["price"])
    except Exception:
        source = "last_candle"

    previous_price = None
    last_candle = None
    try:
        latest_candle_query = (
            select(Candle)
            .where(Candle.symbol == symbol)
            .order_by(Candle.timestamp.desc())
            .limit(1)
        )
        result = await session.execute(latest_candle_query)
        latest_candle = result.scalar_one_or_none()
        if latest_candle:
            last_candle = latest_candle.timestamp.isoformat()
            previous_price = float(latest_candle.close)
            if price is None:
                price = previous_price

            previous_candle_query = (
                select(Candle)
                .where(
                    Candle.symbol == symbol,
                    Candle.timestamp < latest_candle.timestamp,
                )
                .order_by(Candle.timestamp.desc())
                .limit(1)
            )
            result = await session.execute(previous_candle_query)
            previous_candle = result.scalar_one_or_none()
            if previous_candle and previous_candle.close:
                previous_price = float(previous_candle.close)
    except Exception:
        pass

    price_change = None
    price_change_pct = None
    if price is not None and previous_price:
        price_change = price - previous_price
        if previous_price != 0:
            price_change_pct = (price_change / previous_price) * 100

    return {
        "symbol": symbol,
        "current_price": price,
        "price_change": price_change,
        "price_change_pct": price_change_pct,
        "last_candle": last_candle,
        "timestamp": now.isoformat(),
        "source": source,
    }


@router.get("/portfolio")
async def dashboard_portfolio(session: AsyncSession = Depends(get_session)):
    """Return paper trading account summary, open positions, and trade history."""
    from decimal import Decimal
    from app.services.paper_broker import (
        get_account_summary,
        get_open_trades,
        get_trade_history,
        fetch_live_price,
    )

    settings = get_settings()
    ts = await get_trade_settings(session)
    symbol = ts.trading_symbol or settings.trading_symbol

    live_price_dec: Decimal | None = None
    try:
        live_price_dec = await fetch_live_price(symbol, settings.twelve_data_api_key)
    except Exception:
        pass

    summary = await get_account_summary(session)
    open_trades = await get_open_trades(session, live_price=live_price_dec)
    history = await get_trade_history(session, limit=50)

    # Add unrealised P&L to equity
    unrealised = sum(t["unrealised_pnl"] or 0.0 for t in open_trades)
    summary["unrealised_pnl"] = round(unrealised, 2)
    summary["equity"] = round(summary["balance"] + unrealised, 2)

    return {
        "account": summary,
        "open_trades": open_trades,
        "history": history,
    }
