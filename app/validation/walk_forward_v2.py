"""Walk-forward harness for v2 strategies.

Splits a candle DataFrame into rolling (in-sample, out-of-sample) windows,
runs the v2 strategy on each pair, and returns aggregate metrics. Acceptance
criterion: out-of-sample expectancy >= 70% of in-sample.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from app.services.backtester import BacktestRunner
from app.services.metrics_calculator import BacktestMetrics
from app.strategies.base import BaseStrategy


@dataclass
class WalkForwardWindow:
    train_metrics: BacktestMetrics
    test_metrics: BacktestMetrics
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp


@dataclass
class WalkForwardResult:
    windows: list[WalkForwardWindow]
    avg_train_expectancy: float
    avg_test_expectancy: float
    decay: float  # ratio test/train
    passes: bool


class WalkForwardHarness:
    DEFAULT_TRAIN_DAYS = 90
    DEFAULT_TEST_DAYS = 30
    DEFAULT_STEP_DAYS = 30
    PASS_RATIO = 0.70

    def __init__(
        self,
        runner: BacktestRunner | None = None,
        train_days: int = DEFAULT_TRAIN_DAYS,
        test_days: int = DEFAULT_TEST_DAYS,
        step_days: int = DEFAULT_STEP_DAYS,
    ) -> None:
        self.runner = runner or BacktestRunner()
        self.train_days = train_days
        self.test_days = test_days
        self.step_days = step_days

    def run(
        self,
        strategy: BaseStrategy,
        candles: pd.DataFrame,
    ) -> WalkForwardResult:
        if "timestamp" not in candles.columns or candles.empty:
            return WalkForwardResult(
                windows=[], avg_train_expectancy=0.0,
                avg_test_expectancy=0.0, decay=0.0, passes=False,
            )

        c = candles.sort_values("timestamp").reset_index(drop=True)
        ts = pd.to_datetime(c["timestamp"], utc=True)
        first = ts.iloc[0]
        last = ts.iloc[-1]

        windows: list[WalkForwardWindow] = []

        cursor_start = first
        while True:
            train_start = cursor_start
            train_end = train_start + pd.Timedelta(days=self.train_days)
            test_start = train_end
            test_end = test_start + pd.Timedelta(days=self.test_days)

            if test_end > last:
                break

            train_slice = c[(ts >= train_start) & (ts < train_end)]
            test_slice = c[(ts >= test_start) & (ts < test_end)]

            if len(train_slice) < strategy.min_candles + 10:
                cursor_start = cursor_start + pd.Timedelta(days=self.step_days)
                continue
            if len(test_slice) < strategy.min_candles // 2:
                cursor_start = cursor_start + pd.Timedelta(days=self.step_days)
                continue

            train_trades = self.runner.run_rolling_backtest(
                strategy, train_slice, window_days=self.train_days, step_days=self.train_days,
            )
            test_trades = self.runner.run_rolling_backtest(
                strategy, test_slice, window_days=self.test_days, step_days=self.test_days,
            )

            train_metrics = self.runner.metrics_calculator.compute(train_trades)
            test_metrics = self.runner.metrics_calculator.compute(test_trades)

            windows.append(
                WalkForwardWindow(
                    train_metrics=train_metrics,
                    test_metrics=test_metrics,
                    train_start=train_start,
                    train_end=train_end,
                    test_start=test_start,
                    test_end=test_end,
                )
            )

            cursor_start = cursor_start + pd.Timedelta(days=self.step_days)

        if not windows:
            return WalkForwardResult(
                windows=[], avg_train_expectancy=0.0,
                avg_test_expectancy=0.0, decay=0.0, passes=False,
            )

        avg_train = sum(w.train_metrics.expectancy for w in windows) / len(windows)
        avg_test = sum(w.test_metrics.expectancy for w in windows) / len(windows)
        decay = (avg_test / avg_train) if avg_train > 0 else 0.0
        passes = avg_train > 0 and avg_test > 0 and decay >= self.PASS_RATIO

        return WalkForwardResult(
            windows=windows,
            avg_train_expectancy=round(avg_train, 4),
            avg_test_expectancy=round(avg_test, 4),
            decay=round(decay, 3),
            passes=passes,
        )
