"""Dashboard API endpoint — matrix-style operational dashboard."""

import datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, delete, func, select
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
                func.count().filter(
                    (Outcome.result.in_(["tp1_hit", "tp2_hit"]))
                    | ((Outcome.result == "expired") & (Outcome.pnl_pips > 0))
                ).label("wins"),
                func.count().filter(
                    (Outcome.result == "sl_hit")
                    | ((Outcome.result == "expired") & (Outcome.pnl_pips < 0))
                ).label("losses"),
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
    symbol = settings.trading_symbol

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


@router.post("/paper/reset")
async def paper_reset(session: AsyncSession = Depends(get_session)):
    """Full reset: wipe paper trades, signals, outcomes, backtests, optimized params,
    and strategy performance; restore paper account to starting balance."""
    from app.models.backtest_result import BacktestResult
    from app.models.optimized_params import OptimizedParams
    from app.models.outcome import Outcome
    from app.models.paper_trade import PaperAccount, PaperTrade
    from app.models.reversal_log import ReversalLog
    from app.models.signal import Signal
    from app.models.strategy_performance import StrategyPerformance
    from app.services.paper_broker import _PAPER_BALANCE_ID, _STARTING_BALANCE

    # Delete in FK-safe order (children before parents)
    await session.execute(delete(ReversalLog))
    await session.execute(delete(PaperTrade))
    await session.execute(delete(Outcome))
    await session.execute(delete(Signal))
    await session.execute(delete(OptimizedParams))
    await session.execute(delete(BacktestResult))
    await session.execute(delete(StrategyPerformance))

    result = await session.execute(
        select(PaperAccount).where(PaperAccount.id == _PAPER_BALANCE_ID)
    )
    account = result.scalar_one_or_none()
    if account:
        account.balance = _STARTING_BALANCE
        account.starting_balance = _STARTING_BALANCE
    else:
        account = PaperAccount(
            id=_PAPER_BALANCE_ID,
            starting_balance=_STARTING_BALANCE,
            balance=_STARTING_BALANCE,
        )
        session.add(account)

    await session.commit()
    return {"status": "reset", "balance": float(_STARTING_BALANCE)}


@router.post("/signals/reset")
async def signals_reset(session: AsyncSession = Depends(get_session)):
    """Delete all signals and their outcomes."""
    from app.models.outcome import Outcome
    from app.models.signal import Signal

    outcomes_deleted = (await session.execute(delete(Outcome))).rowcount
    signals_deleted = (await session.execute(delete(Signal))).rowcount
    await session.commit()
    return {"status": "reset", "signals_deleted": signals_deleted, "outcomes_deleted": outcomes_deleted}


@router.post("/backtests/reset")
async def backtests_reset(session: AsyncSession = Depends(get_session)):
    """Delete all backtest results and optimized parameters."""
    from app.models.backtest_result import BacktestResult
    from app.models.optimized_params import OptimizedParams

    params_deleted = (await session.execute(delete(OptimizedParams))).rowcount
    backtests_deleted = (await session.execute(delete(BacktestResult))).rowcount
    await session.commit()
    return {"status": "reset", "backtests_deleted": backtests_deleted, "params_deleted": params_deleted}


@router.post("/performance/reset")
async def performance_reset(session: AsyncSession = Depends(get_session)):
    """Delete all strategy performance records."""
    from app.models.strategy_performance import StrategyPerformance

    deleted = (await session.execute(delete(StrategyPerformance))).rowcount
    await session.commit()
    return {"status": "reset", "records_deleted": deleted}


# ---------------------------------------------------------------------------
# V2 engine surface — regime / decisions / expectancy
# ---------------------------------------------------------------------------

@router.get("/regime")
async def regime_state(session: AsyncSession = Depends(get_session)):
    """Return current MarketContext snapshot (regime / HTF / session / news)."""
    from app.engines.market_context import build_market_context

    try:
        ctx = await build_market_context(session)
    except Exception as exc:
        return {"error": str(exc)}
    return {
        "snapshot": ctx.as_snapshot(),
        "liquidity": ctx.liquidity.as_snapshot(),
        "news_next": (
            {
                "title": ctx.news.next_event_title,
                "at": ctx.news.next_event_at.isoformat() if ctx.news.next_event_at else None,
            } if ctx.news.next_event_title else None
        ),
    }


@router.get("/decisions")
async def recent_decisions(
    session: AsyncSession = Depends(get_session),
    limit: int = 50,
):
    """Return the most recent SignalDecision rows (accept + reject)."""
    from app.models.signal_decision import SignalDecision

    rows = (
        await session.execute(
            select(SignalDecision)
            .order_by(SignalDecision.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()

    return [
        {
            "id": r.id,
            "created_at": r.created_at.isoformat(),
            "strategy": r.strategy_name,
            "direction": r.direction,
            "entry_price": float(r.entry_price) if r.entry_price else None,
            "accepted": r.accepted,
            "score": float(r.score) if r.score else None,
            "score_threshold": float(r.score_threshold) if r.score_threshold else None,
            "regime": r.regime,
            "htf_bias": r.htf_bias,
            "session": r.session,
            "news_blocked": r.news_blocked,
            "rejection_reason": r.rejection_reason,
            "score_breakdown": r.score_breakdown,
            "signal_id": r.signal_id,
        }
        for r in rows
    ]


@router.get("/expectancy")
async def expectancy(session: AsyncSession = Depends(get_session)):
    """Return per-strategy expectancy plus attribution slices."""
    from app.analytics.expectancy_tracker import ExpectancyTracker
    from app.analytics.attribution_tracker import AttributionTracker

    by_strat = await ExpectancyTracker().per_strategy(session)
    attr = AttributionTracker()
    by_regime = await attr.by_regime(session)
    by_session = await attr.by_session(session)
    by_score = await attr.by_score_bucket(session)

    return {
        "per_strategy": [
            {
                "strategy": r.strategy_name,
                "trades": r.trades,
                "wins": r.wins,
                "win_rate": r.win_rate,
                "avg_win_r": r.avg_win_r,
                "avg_loss_r": r.avg_loss_r,
                "expectancy_r": r.expectancy_r,
                "profit_factor": r.profit_factor,
            }
            for r in by_strat
        ],
        "by_regime": [vars(b) for b in by_regime],
        "by_session": [vars(b) for b in by_session],
        "by_score": [vars(b) for b in by_score],
    }
