"""Idempotent schema additions for v2 — runs on startup.

`Base.metadata.create_all` only creates missing tables; it does not ALTER
existing ones. Phase A-K added new columns to `outcomes`, `paper_trades`,
and brand-new tables (`signal_decisions`, `news_events`). For existing
deployments we run lightweight `ADD COLUMN IF NOT EXISTS` statements here.

PostgreSQL only. Errors are logged but never fatal.
"""

from __future__ import annotations

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


_ALTERS: list[str] = [
    # outcomes — new analytics fields
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS mae_pts NUMERIC(10,2)",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS mfe_pts NUMERIC(10,2)",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS rr_achieved NUMERIC(6,3)",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS regime_at_entry VARCHAR(20)",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS score_at_entry NUMERIC(4,2)",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS session_at_entry VARCHAR(20)",
    # paper_trades — running excursions
    "ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS mae_pts NUMERIC(10,2)",
    "ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS mfe_pts NUMERIC(10,2)",
]


async def apply_v2_schema_alters(engine: AsyncEngine) -> None:
    """Apply idempotent ADD COLUMN IF NOT EXISTS statements."""
    async with engine.begin() as conn:
        for stmt in _ALTERS:
            try:
                await conn.execute(text(stmt))
            except Exception as exc:
                logger.warning("schema_sync: '{}' failed: {!r}", stmt, exc)
    logger.info("schema_sync: applied {} v2 alters", len(_ALTERS))
