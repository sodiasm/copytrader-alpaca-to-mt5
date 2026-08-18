from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Any, Callable

from .config import Settings
from .models import AccountSnapshot, ConfigurationError, SafetyError


LOGGER = logging.getLogger(__name__)


class AlpacaGateway:
    def __init__(self, settings: Settings):
        try:
            from alpaca.trading.client import TradingClient
        except ImportError as exc:
            raise ConfigurationError(
                "alpaca-py is not installed; run scripts/setup.ps1"
            ) from exc
        self.settings = settings
        self.client = TradingClient(
            settings.alpaca_key,
            settings.alpaca_secret,
            paper=settings.alpaca_paper,
        )

    def account(self) -> AccountSnapshot:
        account = self.client.get_account()
        equity = Decimal(str(account.equity))
        if equity <= 0:
            raise SafetyError("Alpaca account equity must be positive")
        return AccountSnapshot(
            equity=equity,
            account_id=str(account.id),
            is_demo=self.settings.alpaca_paper,
        )

    def positions(self) -> dict[str, Decimal]:
        return {
            str(position.symbol).upper(): Decimal(str(position.qty))
            for position in self.client.get_all_positions()
        }

    def active_tradable_us_assets(self) -> set[str]:
        try:
            assets = self.client.get_all_assets()
        except Exception as exc:
            raise SafetyError(f"could not query Alpaca asset catalog: {exc}") from exc
        return {
            str(asset.symbol).upper()
            for asset in assets
            if str(getattr(asset.asset_class, "value", asset.asset_class)) == "us_equity"
            and str(getattr(asset.status, "value", asset.status)) == "active"
            and bool(asset.tradable)
        }

    def open_orders(self) -> list[dict[str, str]]:
        try:
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest

            orders = self.client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)
            )
        except Exception as exc:
            raise SafetyError(f"could not query Alpaca open orders: {exc}") from exc
        return [
            {
                "id": str(order.id),
                "symbol": str(order.symbol).upper(),
                "side": str(getattr(order.side, "value", order.side)),
                "qty": str(order.qty),
                "filled_qty": str(order.filled_qty),
            }
            for order in orders
        ]

    def submit_market_order(self, symbol: str, quantity: Decimal, side: str) -> str:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        request = MarketOrderRequest(
            symbol=symbol,
            qty=float(quantity),
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        order = self.client.submit_order(order_data=request)
        return str(order.id)

    def close_position(self, symbol: str) -> str:
        order = self.client.close_position(symbol)
        return str(order.id)

    def market_is_open(self) -> bool:
        return bool(self.client.get_clock().is_open)

    def latest_trade_price(self, symbol: str) -> Decimal:
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockLatestTradeRequest

            data_client = StockHistoricalDataClient(
                self.settings.alpaca_key,
                self.settings.alpaca_secret,
            )
            result = data_client.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=symbol)
            )
            trade = result[symbol]
            return Decimal(str(trade.price))
        except Exception as exc:
            raise SafetyError(f"could not get latest Alpaca trade for {symbol}: {exc}") from exc


class AlpacaStream:
    def __init__(self, settings: Settings):
        try:
            from alpaca.trading.stream import TradingStream
        except ImportError as exc:
            raise ConfigurationError(
                "alpaca-py is not installed; run scripts/setup.ps1"
            ) from exc
        self._stream = TradingStream(
            settings.alpaca_key,
            settings.alpaca_secret,
            paper=settings.alpaca_paper,
            raw_data=True,
        )

    def run(self, callback: Callable[[dict[str, Any]], None]) -> None:
        async def handler(update: Any) -> None:
            if hasattr(update, "model_dump"):
                update = update.model_dump(mode="json")
            if not isinstance(update, dict):
                LOGGER.error("unexpected Alpaca update type: %s", type(update).__name__)
                return
            callback(update)
            await asyncio.sleep(0)

        self._stream.subscribe_trade_updates(handler)
        self._stream.run()

    def stop(self) -> None:
        self._stream.stop()

    def wait_until_ready(self, timeout: int = 15) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if bool(getattr(self._stream, "_running", False)):
                return
            time.sleep(0.1)
        raise SafetyError("Alpaca trade-update stream did not become ready")
