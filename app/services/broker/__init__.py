"""Broker integration package.

Provides a pluggable adapter layer for order execution. The concrete
implementation (OANDA) is selected at runtime via config. The system
remains fully functional when broker integration is disabled — signals are
still generated, logged, and notified; only order execution is skipped.
"""

from app.services.broker.base import BrokerAdapter, BrokerAccount, BrokerPosition
from app.services.broker.oanda import OandaAdapter
from app.services.broker.executor import OrderExecutor, PositionSyncer

__all__ = [
    "BrokerAdapter",
    "BrokerAccount",
    "BrokerPosition",
    "OandaAdapter",
    "OrderExecutor",
    "PositionSyncer",
]
