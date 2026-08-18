from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .config import Settings
from .models import (
    AccountSnapshot,
    AmbiguousExecutionError,
    ConfigurationError,
    ExecutionResult,
    SafetyError,
    SymbolSpec,
)


LOGGER = logging.getLogger(__name__)


class Mt5Gateway:
    def __init__(self, settings: Settings):
        try:
            import MetaTrader5 as mt5
        except ImportError as exc:
            raise ConfigurationError(
                "MetaTrader5 is not installed; run scripts/setup.ps1"
            ) from exc
        self.mt5 = mt5
        self.settings = settings
        self._lock = threading.RLock()
        self._connected = False

    def connect(self) -> None:
        with self._lock:
            kwargs: dict[str, Any] = {}
            if self.settings.mt5_login is not None:
                kwargs["login"] = self.settings.mt5_login
            if self.settings.mt5_password:
                kwargs["password"] = self.settings.mt5_password
            if self.settings.mt5_server:
                kwargs["server"] = self.settings.mt5_server
            if not self.mt5.initialize(str(self.settings.mt5_path), **kwargs):
                raise ConfigurationError(
                    f"MT5 initialize failed for {self.settings.mt5_path}: {self.mt5.last_error()}"
                )
            self._connected = True

    def shutdown(self) -> None:
        with self._lock:
            if self._connected:
                self.mt5.shutdown()
                self._connected = False

    def account(self) -> AccountSnapshot:
        with self._lock:
            info = self.mt5.account_info()
            if info is None:
                raise SafetyError(f"MT5 account_info failed: {self.mt5.last_error()}")
            equity = Decimal(str(info.equity))
            if equity <= 0:
                raise SafetyError("MT5 account equity must be positive")
            demo_constant = getattr(self.mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)
            return AccountSnapshot(
                equity=equity,
                account_id=str(info.login),
                is_demo=int(info.trade_mode) == int(demo_constant),
            )

    def terminal_status(self) -> dict[str, Any]:
        with self._lock:
            terminal = self.mt5.terminal_info()
            account = self.mt5.account_info()
            if terminal is None or account is None:
                raise SafetyError(f"MT5 status unavailable: {self.mt5.last_error()}")
            return {
                "connected": bool(terminal.connected),
                "trade_allowed": bool(terminal.trade_allowed and account.trade_allowed),
                "terminal_name": str(terminal.name),
                "terminal_path": str(terminal.path),
                "account_login": str(account.login),
                "account_server": str(account.server),
                "account_trade_mode": int(account.trade_mode),
            }

    def discover_symbols(self, query: str) -> list[dict[str, Any]]:
        needle = query.lower()
        with self._lock:
            symbols = self.mt5.symbols_get()
            if symbols is None:
                raise SafetyError(f"MT5 symbols_get failed: {self.mt5.last_error()}")
            matches = []
            for symbol in symbols:
                haystack = f"{symbol.name} {symbol.description} {symbol.path}".lower()
                if needle in haystack:
                    matches.append(
                        {
                            "name": symbol.name,
                            "description": symbol.description,
                            "path": symbol.path,
                            "currency_profit": symbol.currency_profit,
                            "contract_size": symbol.trade_contract_size,
                            "volume_min": symbol.volume_min,
                            "volume_step": symbol.volume_step,
                            "trade_mode": symbol.trade_mode,
                        }
                    )
            return matches

    def all_symbols(self) -> tuple[Any, ...]:
        with self._lock:
            symbols = self.mt5.symbols_get()
            if symbols is None:
                raise SafetyError(f"MT5 symbols_get failed: {self.mt5.last_error()}")
            return tuple(symbols)

    def symbol_specs(self) -> dict[str, SymbolSpec]:
        return {
            str(info.name): SymbolSpec(
                symbol=str(info.name),
                contract_size=Decimal(str(info.trade_contract_size)),
                volume_min=Decimal(str(info.volume_min)),
                volume_max=Decimal(str(info.volume_max)),
                volume_step=Decimal(str(info.volume_step)),
                point=Decimal(str(info.point)),
                currency_profit=str(info.currency_profit),
                trade_mode=int(info.trade_mode),
            )
            for info in self.all_symbols()
        }

    @property
    def full_trade_mode(self) -> int:
        return int(self.mt5.SYMBOL_TRADE_MODE_FULL)

    @property
    def buy_position_type(self) -> int:
        return int(self.mt5.POSITION_TYPE_BUY)

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        with self._lock:
            info = self.mt5.symbol_info(symbol)
            if info is None:
                raise SafetyError(f"MT5 symbol not found: {symbol}")
            if not info.visible and not self.mt5.symbol_select(symbol, True):
                raise SafetyError(f"could not select MT5 symbol {symbol}: {self.mt5.last_error()}")
            return SymbolSpec(
                symbol=symbol,
                contract_size=Decimal(str(info.trade_contract_size)),
                volume_min=Decimal(str(info.volume_min)),
                volume_max=Decimal(str(info.volume_max)),
                volume_step=Decimal(str(info.volume_step)),
                point=Decimal(str(info.point)),
                currency_profit=str(info.currency_profit),
                trade_mode=int(info.trade_mode),
            )

    def current_price(self, symbol: str, side: str) -> Decimal:
        with self._lock:
            tick = self.mt5.symbol_info_tick(symbol)
            if tick is None:
                raise SafetyError(f"no MT5 tick for {symbol}: {self.mt5.last_error()}")
            price = tick.ask if side == "buy" else tick.bid
            result = Decimal(str(price))
            if result <= 0:
                raise SafetyError(f"invalid MT5 {side} price for {symbol}")
            return result

    def positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.mt5.positions_get(symbol=symbol) if symbol else self.mt5.positions_get()
            if rows is None:
                raise SafetyError(f"MT5 positions_get failed: {self.mt5.last_error()}")
            return [
                {
                    "ticket": int(row.ticket),
                    "symbol": str(row.symbol),
                    "type": int(row.type),
                    "volume": Decimal(str(row.volume)),
                    "price_open": Decimal(str(row.price_open)),
                    "magic": int(row.magic),
                    "comment": str(row.comment),
                }
                for row in rows
            ]

    def long_volume(self, symbol: str) -> Decimal:
        buy_type = int(self.mt5.POSITION_TYPE_BUY)
        return sum(
            (position["volume"] for position in self.positions(symbol) if position["type"] == buy_type),
            Decimal("0"),
        )

    def _filling_type(self, symbol: str) -> int:
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise SafetyError(f"MT5 symbol not found: {symbol}")
        flags = int(info.filling_mode)
        if flags & int(getattr(self.mt5, "SYMBOL_FILLING_IOC", 2)):
            return int(self.mt5.ORDER_FILLING_IOC)
        if flags & int(getattr(self.mt5, "SYMBOL_FILLING_FOK", 1)):
            return int(self.mt5.ORDER_FILLING_FOK)
        return int(self.mt5.ORDER_FILLING_RETURN)

    def _request(
        self,
        *,
        symbol: str,
        side: str,
        volume: Decimal,
        correlation: str,
        position_ticket: int | None = None,
    ) -> dict[str, Any]:
        spec = self.symbol_spec(symbol)
        price = self.current_price(symbol, side)
        deviation_points = max(
            1,
            int(
                (price * Decimal(str(self.settings.max_price_deviation_pct)) / Decimal("100"))
                / spec.point
            ),
        )
        request: dict[str, Any] = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": self.mt5.ORDER_TYPE_BUY if side == "buy" else self.mt5.ORDER_TYPE_SELL,
            "price": float(price),
            "deviation": deviation_points,
            "magic": self.settings.magic,
            "comment": correlation,
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_type(symbol),
        }
        if position_ticket is not None:
            request["position"] = int(position_ticket)
        return request

    def _send_one(
        self,
        *,
        symbol: str,
        side: str,
        volume: Decimal,
        correlation: str,
        position_ticket: int | None = None,
    ) -> ExecutionResult:
        before = self.long_volume(symbol)
        request = self._request(
            symbol=symbol,
            side=side,
            volume=volume,
            correlation=correlation,
            position_ticket=position_ticket,
        )
        check = self.mt5.order_check(request)
        if check is None or int(check.retcode) != 0:
            detail = self.mt5.last_error() if check is None else f"{check.retcode}: {check.comment}"
            raise SafetyError(f"MT5 order_check failed for {symbol}: {detail}")
        result = self.mt5.order_send(request)
        if result is None:
            raise AmbiguousExecutionError(
                f"MT5 order_send returned no result for {correlation}: {self.mt5.last_error()}"
            )
        retcode = int(result.retcode)
        success_codes = {
            int(self.mt5.TRADE_RETCODE_DONE),
            int(getattr(self.mt5, "TRADE_RETCODE_DONE_PARTIAL", -1)),
        }
        if retcode not in success_codes:
            placed = int(getattr(self.mt5, "TRADE_RETCODE_PLACED", -2))
            if retcode == placed:
                raise AmbiguousExecutionError(
                    f"MT5 accepted but did not confirm execution for {correlation}"
                )
            raise SafetyError(f"MT5 order rejected ({retcode}): {result.comment}")

        time.sleep(0.2)
        after = self.long_volume(symbol)
        positional_change = after - before if side == "buy" else before - after
        deal_ticket = str(int(result.deal)) if int(result.deal) else ""
        confirmed = positional_change > 0 and deal_ticket != ""
        if not confirmed:
            confirmed = self.find_correlation(correlation)
        if not confirmed:
            raise AmbiguousExecutionError(
                f"MT5 returned success but deal/position verification failed for {correlation}"
            )
        executed = Decimal(str(result.volume)) if result.volume else positional_change
        return ExecutionResult(
            confirmed=True,
            volume=executed,
            price=Decimal(str(result.price)),
            order_ticket=str(int(result.order)) if int(result.order) else "",
            deal_ticket=deal_ticket,
            retcode=retcode,
            comment=str(result.comment),
        )

    def open_long(self, symbol: str, volume: Decimal, correlation: str) -> ExecutionResult:
        with self._lock:
            spec = self.symbol_spec(symbol)
            remaining = volume
            aggregate = Decimal("0")
            last: ExecutionResult | None = None
            while remaining > 0:
                piece = min(remaining, spec.volume_max)
                last = self._send_one(
                    symbol=symbol,
                    side="buy",
                    volume=piece,
                    correlation=correlation,
                )
                aggregate += last.volume
                remaining -= last.volume
            if last is None:
                raise SafetyError("open volume must be positive")
            return ExecutionResult(
                confirmed=True,
                volume=aggregate,
                price=last.price,
                order_ticket=last.order_ticket,
                deal_ticket=last.deal_ticket,
                retcode=last.retcode,
                comment=last.comment,
            )

    def close_long(self, symbol: str, volume: Decimal, correlation: str) -> ExecutionResult:
        with self._lock:
            remaining = volume
            aggregate = Decimal("0")
            last: ExecutionResult | None = None
            positions = [
                position
                for position in self.positions(symbol)
                if position["type"] == int(self.mt5.POSITION_TYPE_BUY)
            ]
            for position in positions:
                if remaining <= 0:
                    break
                piece = min(remaining, position["volume"])
                last = self._send_one(
                    symbol=symbol,
                    side="sell",
                    volume=piece,
                    correlation=correlation,
                    position_ticket=position["ticket"],
                )
                aggregate += last.volume
                remaining -= last.volume
            if last is None or remaining > Decimal("0.00000001"):
                raise AmbiguousExecutionError(
                    f"could not close requested copier volume {volume} on {symbol}; remaining={remaining}"
                )
            return ExecutionResult(
                confirmed=True,
                volume=aggregate,
                price=last.price,
                order_ticket=last.order_ticket,
                deal_ticket=last.deal_ticket,
                retcode=last.retcode,
                comment=last.comment,
            )

    def find_correlation(self, correlation: str) -> bool:
        with self._lock:
            start = datetime.now(timezone.utc) - timedelta(days=7)
            end = datetime.now(timezone.utc) + timedelta(minutes=1)
            deals = self.mt5.history_deals_get(start, end)
            if deals is None:
                return False
            return any(
                int(deal.magic) == self.settings.magic and str(deal.comment) == correlation
                for deal in deals
            )
