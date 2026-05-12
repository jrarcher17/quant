"""Helpers for loading and updating user-editable trade settings."""

from decimal import Decimal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trade_settings import TradeSettings


class TradeSettingsPayload(BaseModel):
    """Validated trade settings payload used by API and services."""

    risk_per_trade_pct: float = Field(default=0.01, ge=0.001, le=0.05)
    max_sl_pips: float = Field(default=800.0, ge=1.0, le=5000.0)
    tp1_rr: float = Field(default=1.5, ge=0.5, le=10.0)
    tp2_rr: float = Field(default=3.0, ge=0.5, le=20.0)
    min_risk_reward: float = Field(default=1.3, ge=0.5, le=10.0)
    min_confidence: float = Field(default=60.0, ge=0.0, le=100.0)
    max_concurrent_signals: int = Field(default=3, ge=1, le=20)
    daily_loss_limit_pct: float = Field(default=0.02, ge=0.001, le=0.50)
    hedge_min_confidence: float = Field(default=100.0, ge=0.0, le=100.0)
    hedge_risk_multiplier: float = Field(default=0.5, ge=0.0, le=1.0)
    dedup_price_distance_pips: float = Field(default=30.0, ge=0.0, le=5000.0)

    @model_validator(mode="after")
    def validate_targets(self) -> "TradeSettingsPayload":
        """Require TP2 to be at least TP1."""
        if self.tp2_rr < self.tp1_rr:
            raise ValueError("TP2 R multiple must be greater than or equal to TP1")
        return self


def trade_settings_to_payload(settings: TradeSettings) -> TradeSettingsPayload:
    """Convert ORM row to API payload."""
    return TradeSettingsPayload(
        risk_per_trade_pct=float(settings.risk_per_trade_pct),
        max_sl_pips=float(settings.max_sl_pips),
        tp1_rr=float(settings.tp1_rr),
        tp2_rr=float(settings.tp2_rr),
        min_risk_reward=float(settings.min_risk_reward),
        min_confidence=float(settings.min_confidence),
        max_concurrent_signals=int(settings.max_concurrent_signals),
        daily_loss_limit_pct=float(settings.daily_loss_limit_pct),
        hedge_min_confidence=float(settings.hedge_min_confidence),
        hedge_risk_multiplier=float(settings.hedge_risk_multiplier),
        dedup_price_distance_pips=float(settings.dedup_price_distance_pips),
    )


async def get_trade_settings(session: AsyncSession) -> TradeSettingsPayload:
    """Load the singleton settings row, creating defaults if needed."""
    result = await session.execute(select(TradeSettings).where(TradeSettings.id == 1))
    settings = result.scalar_one_or_none()
    if settings is None:
        settings = TradeSettings(id=1)
        session.add(settings)
        await session.commit()
        await session.refresh(settings)

    return trade_settings_to_payload(settings)


async def update_trade_settings(
    session: AsyncSession,
    payload: TradeSettingsPayload,
) -> TradeSettingsPayload:
    """Persist the singleton settings row."""
    result = await session.execute(select(TradeSettings).where(TradeSettings.id == 1))
    settings = result.scalar_one_or_none()
    if settings is None:
        settings = TradeSettings(id=1)
        session.add(settings)

    settings.risk_per_trade_pct = Decimal(str(payload.risk_per_trade_pct))
    settings.max_sl_pips = Decimal(str(payload.max_sl_pips))
    settings.tp1_rr = Decimal(str(payload.tp1_rr))
    settings.tp2_rr = Decimal(str(payload.tp2_rr))
    settings.min_risk_reward = Decimal(str(payload.min_risk_reward))
    settings.min_confidence = Decimal(str(payload.min_confidence))
    settings.max_concurrent_signals = payload.max_concurrent_signals
    settings.daily_loss_limit_pct = Decimal(str(payload.daily_loss_limit_pct))
    settings.hedge_min_confidence = Decimal(str(payload.hedge_min_confidence))
    settings.hedge_risk_multiplier = Decimal(str(payload.hedge_risk_multiplier))
    settings.dedup_price_distance_pips = Decimal(
        str(payload.dedup_price_distance_pips)
    )

    await session.commit()
    await session.refresh(settings)
    return trade_settings_to_payload(settings)
