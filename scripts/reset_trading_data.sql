-- Reset all trading data so the system starts fresh for a new symbol.
--
-- Deletes:  broker_orders, outcomes, signals, candles,
--           backtest_results, optimized_params, strategy_performance
--
-- Keeps:    trade_settings (your risk config + chosen symbol)
--           strategies     (strategy registry rows)

-- Clear any broken transaction state from a previous failure
ROLLBACK;

-- Delete in FK-safe order (children before parents)
DELETE FROM broker_orders   WHERE true;
DELETE FROM outcomes        WHERE true;
DELETE FROM signals         WHERE true;
DELETE FROM candles         WHERE true;
DELETE FROM backtest_results     WHERE true;
DELETE FROM optimized_params     WHERE true;
DELETE FROM strategy_performance WHERE true;

-- Confirm row counts (should all be 0)
SELECT 'broker_orders'           AS tbl, COUNT(*) AS rows FROM broker_orders
UNION ALL
SELECT 'outcomes',                        COUNT(*) FROM outcomes
UNION ALL
SELECT 'signals',                         COUNT(*) FROM signals
UNION ALL
SELECT 'candles',                         COUNT(*) FROM candles
UNION ALL
SELECT 'backtest_results',                COUNT(*) FROM backtest_results
UNION ALL
SELECT 'optimized_params',                COUNT(*) FROM optimized_params
UNION ALL
SELECT 'strategy_performance',            COUNT(*) FROM strategy_performance;
