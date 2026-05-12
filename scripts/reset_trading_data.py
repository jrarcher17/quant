"""Reset all trading data so the system starts fresh for a new symbol.

Deletes:
  - candles              (all symbols)
  - signals              (all)
  - outcomes             (all)
  - broker_orders        (all)
  - backtest_results     (all)
  - optimized_params     (all)
  - strategy_performance (all)

Keeps:
  - trade_settings  (your risk config and symbol choice)
  - strategies      (strategy registry rows)

Run from the repo root:
    python scripts/reset_trading_data.py
"""

import asyncio
import sys
from pathlib import Path

# Allow imports from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from app.database import async_session_factory


TABLES_TO_CLEAR = [
    "broker_orders",
    "outcomes",
    "signals",
    "candles",
    "backtest_results",
    "optimized_params",
    "strategy_performance",
]


async def reset() -> None:
    async with async_session_factory() as session:
        total = {}
        for table in TABLES_TO_CLEAR:
            result = await session.execute(text(f"SELECT COUNT(*) FROM {table}"))
            total[table] = result.scalar()

        print("\nRows to be deleted:")
        for table, count in total.items():
            print(f"  {table:<30} {count:>8,}")

        confirm = input("\nType YES to confirm deletion: ").strip()
        if confirm != "YES":
            print("Aborted — no data was changed.")
            return

        for table in TABLES_TO_CLEAR:
            await session.execute(text(f"DELETE FROM {table}"))
            print(f"  Cleared {table}")

        await session.commit()
        print("\nDone. All trading data cleared. The system will start fresh on next deploy/restart.")


if __name__ == "__main__":
    asyncio.run(reset())
