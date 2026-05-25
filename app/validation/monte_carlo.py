"""Monte Carlo harness — bootstrap-resamples the trade R-distribution.

Useful sanity test: even if the *order* of trades is shuffled, does the
strategy still produce profitable distributions? Returns the 5th-percentile
expectancy and max-drawdown bands.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass
class MonteCarloResult:
    iters: int
    expectancy_p05: float
    expectancy_p50: float
    expectancy_p95: float
    max_drawdown_p05: float
    max_drawdown_p50: float
    max_drawdown_p95: float
    survival_rate: float  # fraction of runs with positive ending equity
    passes: bool


class MonteCarloHarness:
    SURVIVAL_THRESHOLD = 0.95

    def __init__(self, iters: int = 5000, seed: int = 42) -> None:
        self.iters = iters
        self._rng = random.Random(seed)

    def run(self, r_multiples: list[float]) -> MonteCarloResult:
        if not r_multiples:
            return MonteCarloResult(
                iters=0,
                expectancy_p05=0.0, expectancy_p50=0.0, expectancy_p95=0.0,
                max_drawdown_p05=0.0, max_drawdown_p50=0.0, max_drawdown_p95=0.0,
                survival_rate=0.0, passes=False,
            )

        n = len(r_multiples)
        expectancies: list[float] = []
        drawdowns: list[float] = []
        survivals = 0

        for _ in range(self.iters):
            sample = [self._rng.choice(r_multiples) for _ in range(n)]
            equity = 0.0
            peak = 0.0
            max_dd = 0.0
            for r in sample:
                equity += r
                if equity > peak:
                    peak = equity
                dd = peak - equity
                if dd > max_dd:
                    max_dd = dd

            exp = sum(sample) / n
            expectancies.append(exp)
            drawdowns.append(max_dd)
            if equity > 0:
                survivals += 1

        expectancies.sort()
        drawdowns.sort()

        def pct(arr, p):
            if not arr:
                return 0.0
            idx = max(0, min(len(arr) - 1, int(math.floor(p * len(arr)))))
            return arr[idx]

        survival_rate = survivals / self.iters

        return MonteCarloResult(
            iters=self.iters,
            expectancy_p05=round(pct(expectancies, 0.05), 4),
            expectancy_p50=round(pct(expectancies, 0.50), 4),
            expectancy_p95=round(pct(expectancies, 0.95), 4),
            max_drawdown_p05=round(pct(drawdowns, 0.05), 4),
            max_drawdown_p50=round(pct(drawdowns, 0.50), 4),
            max_drawdown_p95=round(pct(drawdowns, 0.95), 4),
            survival_rate=round(survival_rate, 4),
            passes=survival_rate >= self.SURVIVAL_THRESHOLD and pct(expectancies, 0.05) > 0,
        )
