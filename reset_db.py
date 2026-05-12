"""Full database reset — clears all trading data, keeps trade_settings.

Usage:
    python reset_db.py

Reads DATABASE_URL from .env (or environment). On completion the app will
re-bootstrap strategies, candles, and backtests on next startup.

Tables cleared:
    paper_trades, paper_account, outcomes, strategy_performance,
    optimized_params, backtest_results, signals, candles, strategies

Tables preserved:
    trade_settings, alembic_version
"""

import asyncio
import sys
from pathlib import Path

# Load .env so DATABASE_URL is available without exporting manually
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import os

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    sys.exit("ERROR: DATABASE_URL is not set")

# Normalise to asyncpg driver
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

# asyncpg rejects any query params it doesn't know (sslmode, channel_binding,
# etc.). Strip them all and pass ssl=True directly if the original URL had any
# SSL-related params (Railway always needs SSL).
from urllib.parse import urlparse, parse_qs, urlunparse
_parsed = urlparse(DATABASE_URL)
_qs = parse_qs(_parsed.query)
_needs_ssl = "sslmode" in _qs or "ssl" in _qs
DATABASE_URL = urlunparse(_parsed._replace(query=""))
_connect_args = {"ssl": True} if _needs_ssl else {}

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

# Tables to wipe, ordered to satisfy FK constraints (children first).
# paper_trades / paper_account are excluded here — they don't exist until the
# pending migration runs; they'll be created fresh (at $1,000 balance) on
# next deploy, so no pre-clearing needed.
# RESTART IDENTITY resets serial sequences back to 1.
TRUNCATE_SQL = """
TRUNCATE TABLE
    outcomes,
    strategy_performance,
    optimized_params,
    backtest_results,
    signals,
    candles,
    strategies
RESTART IDENTITY CASCADE;
"""


async def reset() -> None:
    engine = create_async_engine(DATABASE_URL, echo=False, connect_args=_connect_args)
    async with engine.begin() as conn:
        # Confirm with the user before doing anything destructive
        print("\n⚠️  This will permanently delete ALL trading data in the production database.")
        print("Tables to be cleared: outcomes, strategy_performance, optimized_params,")
        print("  backtest_results, signals, candles, strategies")
        print("  (paper_trades / paper_account don't exist yet — created fresh on next deploy)")
        print("Preserved: trade_settings, alembic_version\n")
        answer = input("Type 'yes' to confirm: ").strip().lower()
        if answer != "yes":
            print("Aborted.")
            return

        await conn.execute(text(TRUNCATE_SQL))
        print("\nDone. All trading data has been cleared.")
        print("Redeploy (or restart) the app — it will re-bootstrap strategies,")
        print("candles, and backtests automatically on startup.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(reset())
