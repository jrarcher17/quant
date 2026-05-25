"""AttributionTracker — slice outcomes by regime / session / score band.

Used by the dashboard "Attribution" tab to spotlight which regimes,
sessions, and score buckets actually produce profitable trades.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outcome import Outcome


@dataclass
class AttributionBucket:
    label: str
    trades: int
    wins: int
    win_rate: float
    avg_rr: float
    expectancy_r: float


class AttributionTracker:
    async def by_regime(self, session: AsyncSession) -> list[AttributionBucket]:
        rows = (
            await session.execute(
                select(
                    Outcome.regime_at_entry,
                    Outcome.result,
                    Outcome.rr_achieved,
                )
            )
        ).all()
        return self._bucketise(rows, "unknown_regime")

    async def by_session(self, session: AsyncSession) -> list[AttributionBucket]:
        rows = (
            await session.execute(
                select(
                    Outcome.session_at_entry,
                    Outcome.result,
                    Outcome.rr_achieved,
                )
            )
        ).all()
        return self._bucketise(rows, "unknown_session")

    async def by_score_bucket(self, session: AsyncSession) -> list[AttributionBucket]:
        rows = (
            await session.execute(
                select(
                    Outcome.score_at_entry,
                    Outcome.result,
                    Outcome.rr_achieved,
                )
            )
        ).all()
        bucketed = []
        for score, result, rr in rows:
            label = self._score_label(score)
            bucketed.append((label, result, rr))
        return self._bucketise(bucketed, "no_score")

    def _score_label(self, score) -> str:
        if score is None:
            return "no_score"
        s = float(score)
        if s >= 8.5:
            return "elite (8.5-10)"
        if s >= 7.5:
            return "strong (7.5-8.5)"
        if s >= 6.5:
            return "ok (6.5-7.5)"
        return "marginal (<6.5)"

    def _bucketise(self, rows, default_label: str) -> list[AttributionBucket]:
        groups: dict[str, list] = {}
        for label, result, rr in rows:
            key = label or default_label
            groups.setdefault(key, []).append((result, rr))

        out: list[AttributionBucket] = []
        for key, samples in groups.items():
            wins = [s for s in samples if s[0] in ("tp1_hit", "tp2_hit")]
            losses = [s for s in samples if s[0] == "sl_hit"]
            n = len(samples)
            wr = (len(wins) / n) if n else 0.0
            rrs = [float(r[1]) for r in samples if r[1] is not None]
            avg_rr = sum(rrs) / len(rrs) if rrs else 0.0

            avg_win = (
                sum(float(r[1]) for r in wins if r[1] is not None) / len(wins)
                if wins else 1.0
            )
            avg_loss = (
                sum(abs(float(r[1])) for r in losses if r[1] is not None) / len(losses)
                if losses else 1.0
            )
            expectancy = wr * avg_win - (1 - wr) * avg_loss

            out.append(
                AttributionBucket(
                    label=key,
                    trades=n,
                    wins=len(wins),
                    win_rate=round(wr, 4),
                    avg_rr=round(avg_rr, 3),
                    expectancy_r=round(expectancy, 3),
                )
            )
        out.sort(key=lambda b: b.expectancy_r, reverse=True)
        return out
