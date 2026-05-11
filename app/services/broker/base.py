"""Abstract broker adapter interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass
class BrokerAccount:
    """Snapshot of account state returned by the broker."""

    balance: Decimal
    equity: Decimal
    unrealized_pnl: Decimal
    margin_used: Decimal
    margin_available: Decimal
    currency: str
    mode: str  # "practice" or "live"


@dataclass
class BrokerPosition:
    """An open position currently held at the broker."""

    trade_id: str
    instrument: str
    units: Decimal          # positive = long, negative = short
    open_price: Decimal
    current_price: Decimal
    unrealized_pnl: Decimal
    stop_loss: Optional[Decimal]
    take_profit: Optional[Decimal]


@dataclass
class PlaceOrderResult:
    """Result returned after placing an order."""

    success: bool
    order_id: Optional[str]
    trade_id: Optional[str]    # set once the market order is filled
    fill_price: Optional[Decimal]
    error: Optional[str]


class BrokerAdapter(ABC):
    """Abstract interface every broker implementation must satisfy."""

    @abstractmethod
    async def place_market_order(
        self,
        instrument: str,
        units: Decimal,
        stop_loss: Decimal,
        take_profit: Decimal,
        client_order_id: Optional[str] = None,
    ) -> PlaceOrderResult:
        """Place a market order with attached SL and TP.

        Parameters
        ----------
        instrument:
            Broker-specific instrument code (e.g. "XAU_USD" for OANDA).
        units:
            Positive for BUY, negative for SELL. Quantity in the broker's
            native unit (1 unit = 1 troy oz for XAU_USD on OANDA).
        stop_loss:
            Absolute price level for the stop loss.
        take_profit:
            Absolute price level for the take profit (TP1 is used so that
            the broker manages the first target; TP2 is tracked internally).
        client_order_id:
            Optional tag passed to the broker for reconciliation.
        """

    @abstractmethod
    async def close_trade(self, trade_id: str) -> bool:
        """Close an open trade by its broker trade ID.

        Returns True if the trade was closed successfully, False otherwise.
        """

    @abstractmethod
    async def get_account(self) -> BrokerAccount:
        """Return a current snapshot of the account (balance, equity, P&L)."""

    @abstractmethod
    async def get_open_positions(self) -> list[BrokerPosition]:
        """Return all currently open positions."""
