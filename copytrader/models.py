from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


class CopyTraderError(RuntimeError):
    """Base error for controlled copier failures."""


class ConfigurationError(CopyTraderError):
    pass


class SafetyError(CopyTraderError):
    pass


class AmbiguousExecutionError(SafetyError):
    pass


def decimal_value(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SafetyError(f"invalid {field}: {value!r}") from exc
    if not result.is_finite():
        raise SafetyError(f"non-finite {field}: {value!r}")
    return result


@dataclass(frozen=True)
class SymbolMapping:
    source: str
    target: str
    asset_type: str


@dataclass(frozen=True)
class TradeEvent:
    execution_id: str
    event_type: str
    order_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    position_quantity: Decimal | None
    timestamp: str
    raw: dict[str, Any]

    @classmethod
    def from_update(cls, update: dict[str, Any]) -> "TradeEvent":
        payload = update.get("data", update)
        event_type = str(payload.get("event", "")).lower()
        if event_type not in {"fill", "partial_fill"}:
            raise SafetyError(f"unsupported event type: {event_type or '<missing>'}")
        order = payload.get("order") or {}
        symbol = str(order.get("symbol", "")).upper()
        side = str(order.get("side", "")).lower()
        if not symbol or side not in {"buy", "sell"}:
            raise SafetyError("fill is missing a valid symbol or side")
        execution_id = str(
            payload.get("execution_id")
            or payload.get("event_id")
            or ""
        )
        if not execution_id:
            raise SafetyError("fill is missing execution_id/event_id")
        qty = decimal_value(payload.get("qty"), "fill quantity")
        price = decimal_value(payload.get("price"), "fill price")
        if qty <= 0 or price <= 0:
            raise SafetyError("fill quantity and price must be positive")
        position_raw = payload.get("position_qty")
        position_qty = (
            decimal_value(position_raw, "position quantity")
            if position_raw is not None
            else None
        )
        timestamp = str(
            payload.get("timestamp")
            or payload.get("at")
            or datetime.now(timezone.utc).isoformat()
        )
        return cls(
            execution_id=execution_id,
            event_type=event_type,
            order_id=str(order.get("id", "")),
            symbol=symbol,
            side=side,
            quantity=qty,
            price=price,
            position_quantity=position_qty,
            timestamp=timestamp,
            raw=payload,
        )


@dataclass(frozen=True)
class AccountSnapshot:
    equity: Decimal
    account_id: str
    is_demo: bool


@dataclass(frozen=True)
class SymbolSpec:
    symbol: str
    contract_size: Decimal
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    point: Decimal
    currency_profit: str
    trade_mode: int


@dataclass(frozen=True)
class ExecutionResult:
    confirmed: bool
    volume: Decimal
    price: Decimal
    order_ticket: str
    deal_ticket: str
    retcode: int
    comment: str
