-- Reset all trading data so the system starts fresh for a new symbol.
--
-- Deletes:  broker_orders, outcomes, signals, candles,
--           backtest_results, optimized_params, strategy_performance
--
-- Keeps:    trade_settings (your risk config + chosen symbol)
--           strategies     (strategy registry rows)
--
-- Run this in the Neon SQL console or via psql:
--   psql $DATABASE_URL -f scripts/reset_trading_data.sql

BEGIN;

DELETE FROM broker_orders;
DELETE FROM outcomes;
DELETE FROM signals;
DELETE FROM candles;
DELETE FROM backtest_results;
DELETE FROM optimized_params;
DELETE FROM strategy_performance;

COMMIT;

-- Confirm row counts (should all be 0)
SELECT 'broker_orders'        AS tbl, COUNT(*) AS rows FROM broker_orders
UNION ALL
SELECT 'outcomes',                     COUNT(*) FROM outcomes
UNION ALL
SELECT 'signals',                      COUNT(*) FROM signals
UNION ALL
SELECT 'candles',                      COUNT(*) FROM candles
UNION ALL
SELECT 'backtest_results',             COUNT(*) FROM backtest_results
UNION ALL
SELECT 'optimized_params',             COUNT(*) FROM optimized_params
UNION ALL
SELECT 'strategy_performance',         COUNT(*) FROM strategy_performance;
