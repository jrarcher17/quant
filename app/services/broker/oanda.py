"""OANDA v20 REST API broker adapter.

Uses oandapyV20 (pure-HTTP, Linux-compatible) to execute orders against
either the practice (paper) or live environment.

Paper trading:  OANDA_MODE=practice  →  api-fxpractice.oanda.com
Live trading:   OANDA_MODE=live      →  api-fxtrade.oanda.com

The instrument for gold is "XAU_USD" in OANDA's notation; 1 unit = 1 troy oz.
"""

from decimal import Decimal
from typing import Optional

from loguru import logger

from app.services.broker.base import (
    BrokerAccount,
    BrokerAdapter,
    BrokerPosition,
    PlaceOrderResult,
)

# oandapyV20 is an optional dependency — guarded so import errors surface
# clearly at runtime rather than at module load.
try:
    import oandapyV20
    import oandapyV20.endpoints.accounts as accounts_ep
    import oandapyV20.endpoints.orders as orders_ep
    import oandapyV20.endpoints.trades as trades_ep
    import oandapyV20.endpoints.positions as positions_ep
    _OANDA_AVAILABLE = True
except ImportError:
    _OANDA_AVAILABLE = False
    logger.warning(
        "oandapyV20 not installed — OANDA broker adapter disabled. "
        "Run: pip install oandapyV20"
    )


# Map between our internal symbol (XAUUSD) and OANDA instrument codes
_SYMBOL_TO_INSTRUMENT: dict[str, str] = {
    "XAUUSD": "XAU_USD",
    "XAU_USD": "XAU_USD",
    "EURUSD": "EUR_USD",
    "GBPUSD": "GBP_USD",
    "USDJPY": "USD_JPY",
}


def symbol_to_oanda(symbol: str) -> str:
    """Convert an internal symbol string to OANDA instrument notation."""
    upper = symbol.upper().replace("/", "").replace("-", "")
    return _SYMBOL_TO_INSTRUMENT.get(upper, upper[:3] + "_" + upper[3:])


class OandaAdapter(BrokerAdapter):
    """OANDA v20 REST adapter.

    Parameters
    ----------
    api_token:
        Personal access token generated in the OANDA Account Management Portal.
    account_id:
        The numeric account ID shown in the portal (e.g. "001-001-1234567-001").
    mode:
        "practice" for the demo environment, "live" for real money.
    """

    def __init__(self, api_token: str, account_id: str, mode: str = "practice") -> None:
        if not _OANDA_AVAILABLE:
            raise RuntimeError(
                "oandapyV20 is not installed. "
                "Add it to requirements.txt and reinstall dependencies."
            )
        self._account_id = account_id
        self._mode = mode
        environment = "practice" if mode == "practice" else "live"
        self._client = oandapyV20.API(access_token=api_token, environment=environment)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def place_market_order(
        self,
        instrument: str,
        units: Decimal,
        stop_loss: Decimal,
        take_profit: Decimal,
        client_order_id: Optional[str] = None,
    ) -> PlaceOrderResult:
        """Submit a market order with attached stop-loss and take-profit."""
        data: dict = {
            "order": {
                "type": "MARKET",
                "instrument": instrument,
                "units": str(units),
                "timeInForce": "FOK",
                "positionFill": "DEFAULT",
                "stopLossOnFill": {
                    "price": f"{float(stop_loss):.5f}",
                    "timeInForce": "GTC",
                },
                "takeProfitOnFill": {
                    "price": f"{float(take_profit):.5f}",
                    "timeInForce": "GTC",
                },
            }
        }
        if client_order_id:
            data["order"]["clientExtensions"] = {"id": client_order_id[:128]}

        try:
            request = orders_ep.OrderCreate(self._account_id, data=data)
            response = self._client.request(request)

            order_id = response.get("orderCreateTransaction", {}).get("id")
            fill = response.get("orderFillTransaction", {})
            trade_id = fill.get("tradeOpened", {}).get("tradeID") or fill.get("tradeID")
            fill_price_str = fill.get("price")
            fill_price = Decimal(fill_price_str) if fill_price_str else None

            logger.info(
                "OANDA order placed | instrument={} units={} order_id={} trade_id={} fill={}",
                instrument,
                units,
                order_id,
                trade_id,
                fill_price,
            )
            return PlaceOrderResult(
                success=True,
                order_id=order_id,
                trade_id=trade_id,
                fill_price=fill_price,
                error=None,
            )

        except Exception as exc:
            err = str(exc)
            logger.error(
                "OANDA order failed | instrument={} units={} error={}",
                instrument,
                units,
                err,
            )
            return PlaceOrderResult(
                success=False,
                order_id=None,
                trade_id=None,
                fill_price=None,
                error=err[:500],
            )

    async def close_trade(self, trade_id: str) -> bool:
        """Close an open trade by its OANDA trade ID."""
        try:
            request = trades_ep.TradeClose(self._account_id, tradeID=trade_id)
            self._client.request(request)
            logger.info("OANDA trade closed | trade_id={}", trade_id)
            return True
        except Exception as exc:
            logger.error("OANDA close_trade failed | trade_id={} error={}", trade_id, exc)
            return False

    async def get_account(self) -> BrokerAccount:
        """Return current account snapshot (balance, equity, P&L)."""
        request = accounts_ep.AccountSummary(self._account_id)
        response = self._client.request(request)
        acct = response.get("account", {})
        return BrokerAccount(
            balance=Decimal(str(acct.get("balance", "0"))),
            equity=Decimal(str(acct.get("NAV", acct.get("balance", "0")))),
            unrealized_pnl=Decimal(str(acct.get("unrealizedPL", "0"))),
            margin_used=Decimal(str(acct.get("marginUsed", "0"))),
            margin_available=Decimal(str(acct.get("marginAvailable", "0"))),
            currency=acct.get("currency", "USD"),
            mode=self._mode,
        )

    async def get_open_positions(self) -> list[BrokerPosition]:
        """Return all open positions from OANDA."""
        positions: list[BrokerPosition] = []
        try:
            # Get open trades (individual position entries, not net positions)
            request = trades_ep.OpenTrades(self._account_id)
            response = self._client.request(request)
            for trade in response.get("trades", []):
                units = Decimal(str(trade.get("currentUnits", "0")))
                open_price = Decimal(str(trade.get("price", "0")))
                unrealized_pnl = Decimal(str(trade.get("unrealizedPL", "0")))
                current_price = open_price  # approximation; price not in trade summary

                sl_order = trade.get("stopLossOrder", {})
                tp_order = trade.get("takeProfitOrder", {})
                sl = Decimal(sl_order["price"]) if sl_order.get("price") else None
                tp = Decimal(tp_order["price"]) if tp_order.get("price") else None

                positions.append(
                    BrokerPosition(
                        trade_id=str(trade.get("id", "")),
                        instrument=trade.get("instrument", ""),
                        units=units,
                        open_price=open_price,
                        current_price=current_price,
                        unrealized_pnl=unrealized_pnl,
                        stop_loss=sl,
                        take_profit=tp,
                    )
                )
        except Exception as exc:
            logger.error("OANDA get_open_positions failed | error={}", exc)
        return positions
