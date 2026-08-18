from __future__ import annotations

from decimal import Decimal, ROUND_DOWN

from .models import SafetyError, SymbolSpec


ZERO = Decimal("0")


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise SafetyError("volume step must be positive")
    if value <= 0:
        return ZERO
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def buy_volume(
    *,
    fill_quantity: Decimal,
    fill_price: Decimal,
    source_equity: Decimal,
    target_equity: Decimal,
    target_price: Decimal,
    spec: SymbolSpec,
    residual_lots: Decimal,
) -> tuple[Decimal, Decimal]:
    values = (fill_quantity, fill_price, source_equity, target_equity, target_price)
    if any(value <= 0 for value in values):
        raise SafetyError("sizing inputs must be positive")
    theoretical = (
        fill_quantity
        * fill_price
        * (target_equity / source_equity)
        / (target_price * spec.contract_size)
        + residual_lots
    )
    executable = floor_to_step(theoretical, spec.volume_step)
    if executable < spec.volume_min:
        return ZERO, theoretical
    return executable, theoretical - executable


def sell_volume(
    *,
    fill_quantity: Decimal,
    managed_source_quantity: Decimal,
    managed_target_volume: Decimal,
    residual_lots: Decimal,
    spec: SymbolSpec,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    if fill_quantity <= 0:
        raise SafetyError("sell quantity must be positive")
    if managed_source_quantity <= 0:
        return ZERO, ZERO, managed_target_volume, residual_lots
    effective_sell = min(fill_quantity, managed_source_quantity)
    remaining_source = managed_source_quantity - effective_sell
    if remaining_source == 0:
        return managed_target_volume, ZERO, ZERO, ZERO
    remaining_fraction = remaining_source / managed_source_quantity
    desired_target = floor_to_step(
        managed_target_volume * remaining_fraction,
        spec.volume_step,
    )
    close_volume = managed_target_volume - desired_target
    remaining_residual = residual_lots * remaining_fraction
    return close_volume, remaining_source, desired_target, remaining_residual


def price_deviation_pct(source_price: Decimal, target_price: Decimal) -> Decimal:
    if source_price <= 0 or target_price <= 0:
        raise SafetyError("prices must be positive")
    return abs(target_price - source_price) / source_price * Decimal("100")
