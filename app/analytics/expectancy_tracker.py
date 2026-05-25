"""ExpectancyTracker — computes per-strategy expectancy from Outcome rows.

expectancy = (win_rate * avg_win_R) - ((1 - win_rate) * avg_loss_R)

avg_win_R / avg_loss_R use rr_achieved when present, else fall back to a
fixed 1.0/-1.0 approximation.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outcome import Outcome
from app.models.signal import Signal


@dataclass
class ExpectancyRow:
    strategy_name: str
    trades: int
    wins: int
    win_rate: float
    avg_win_r: float
    avg_loss_r: float
    expectancy_r: float
    profit_factor: float


class ExpectancyTracker:
    async def per_strategy(self, session: AsyncSession) -> list[ExpectancyRow]:
        rows = (
            await session.execute(
                select(
                    Signal.strategy_id,
                    Signal.direction,
                    Outcome.result,
                    Outcome.pnl_pips,
                    Outcome.rr_achieved,
                ).join(Outcome, Outcome.signal_id == Signal.id)
            )
        ).all()

        # group by strategy_id
        per_strat: dict[int, list[tuple[str, float | None, float]]] = {}
        for sid, _direction, result, pnl_pips, rr in rows:
            per_strat.setdefault(sid, []).append((result, float(rr) if rr is not None else None, float(pnl_pips or 0)))

        # resolve strategy_id -> name
        from app.models.strategy import Strategy as StrategyModel
        strat_rows = (
            await session.execute(select(StrategyModel.id, StrategyModel.name))
        ).all()
        name_by_id = {sid: name for sid, name in strat_rows}

        out: list[ExpectancyRow] = []
        for sid, samples in per_strat.items():
            if not samples:
                continue
            wins = [s for s in samples if s[0] in ("tp1_hit", "tp2_hit")]
            losses = [s for s in samples if s[0] == "sl_hit"]
            n = len(samples)
            win_rate = (len(wins) / n) if n else 0.0

            def avg_r(records, default_r):
                if not records:
                    return 0.0
                rs = [r for _res, r, _pp in records if r is not None]
                if rs:
                    return sum(rs) / len(rs)
                return default_r

            avg_win_r = avg_r(wins, default_r=1.0)
            avg_loss_r = abs(avg_r(losses, default_r=-1.0))

            expectancy = win_rate * avg_win_r - (1 - win_rate) * avg_loss_r

            gross_profit = sum(
                r for _res, r, _pp in wins if r is not None
            ) or len(wins) * 1.0
            gross_loss = sum(
                abs(r) for _res, r, _pp in losses if r is not None
            ) or len(losses) * 1.0
            profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

            out.append(
                ExpectancyRow(
                    strategy_name=name_by_id.get(sid, f"strat_{sid}"),
                    trades=n,
                    wins=len(wins),
                    win_rate=round(win_rate, 4),
                    avg_win_r=round(avg_win_r, 3),
                    avg_loss_r=round(avg_loss_r, 3),
                    expectancy_r=round(expectancy, 3),
                    profit_factor=round(profit_factor, 3) if profit_factor != float("inf") else 99.99,
                )
            )

        out.sort(key=lambda r: r.expectancy_r, reverse=True)
        return out
