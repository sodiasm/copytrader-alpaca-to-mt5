from __future__ import annotations

import json
import logging
import queue
import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .alpaca_adapter import AlpacaGateway
from .config import Settings
from .models import AmbiguousExecutionError, SafetyError, TradeEvent
from .mt5_adapter import Mt5Gateway
from .sizing import buy_volume, price_deviation_pct, sell_volume
from .storage import StateStore


LOGGER = logging.getLogger(__name__)


class CopyEngine:
    def __init__(
        self,
        settings: Settings,
        store: StateStore,
        alpaca: AlpacaGateway,
        mt5: Mt5Gateway,
    ):
        self.settings = settings
        self.store = store
        self.alpaca = alpaca
        self.mt5 = mt5
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._monitor = threading.Thread(target=self._monitor_loop, daemon=True)
        self._stop_event = threading.Event()
        self._drift_observations: dict[str, str] = {}
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.store.set_state("stream_state", "starting")
        self._worker.start()
        self._monitor.start()
        for raw_event in self.store.pending_events():
            self._queue.put(raw_event)

    def stop(self) -> None:
        if not self._started:
            return
        self._stop_event.set()
        self._queue.put(None)
        self._worker.join(timeout=10)
        self._monitor.join(timeout=2)
        self.store.set_state("stream_state", "stopped")

    def enqueue(self, update: dict[str, Any]) -> None:
        payload = update.get("data", update)
        if str(payload.get("event", "")).lower() not in {"fill", "partial_fill"}:
            return
        self._queue.put(update)

    def _worker_loop(self) -> None:
        self.store.set_state("stream_state", "connected")
        while True:
            update = self._queue.get()
            if update is None:
                self._queue.task_done()
                return
            try:
                self.process(update)
            except Exception:
                LOGGER.exception("unexpected event-processing failure")
            finally:
                self._queue.task_done()

    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(self.settings.poll_interval_seconds):
            if self._queue.unfinished_tasks:
                continue
            try:
                self._check_drift_once()
            except Exception as exc:
                reason = f"state monitor failed: {type(exc).__name__}: {exc}"
                LOGGER.error(reason)
                self.store.pause_all(reason)
                self.store.set_state("stream_state", "monitor_error_paused")

    def _check_drift_once(self) -> None:
        source_positions = self.alpaca.positions()
        target_positions = self.mt5.positions()
        target_longs = _long_volumes(target_positions, self.mt5.buy_position_type)
        for source, mapping in self.settings.enabled_mappings.items():
            managed_source, managed_target, _ = self.store.allocation(source, mapping.target)
            live_source = source_positions.get(source, Decimal("0"))
            live_target = target_longs.get(mapping.target, Decimal("0"))
            signature = (
                f"alpaca={live_source}/managed={managed_source};"
                f"mt5={live_target}/managed={managed_target}"
            )
            if live_source == managed_source and live_target == managed_target:
                self._drift_observations.pop(source, None)
                continue
            if self._drift_observations.get(source) == signature:
                self.store.pause(source, f"persistent position drift: {signature}")
                self.store.set_state("stream_state", "drift_paused")
                LOGGER.error("persistent position drift on %s: %s", source, signature)
            else:
                self._drift_observations[source] = signature

    def process(self, update: dict[str, Any]) -> None:
        event = TradeEvent.from_update(update)
        is_new = self.store.record_event(event)
        if not is_new and self.store.event_status(event.execution_id) not in {
            "received",
            "processing",
        }:
            LOGGER.info("duplicate execution ignored: %s", event.execution_id)
            return
        self.store.update_event(event.execution_id, "processing")

        mapping = self.settings.enabled_mappings.get(event.symbol)
        if mapping is None:
            reason = f"no reviewed catalog mapping for {event.symbol}"
            self.store.pause(event.symbol, reason)
            self.store.update_event(event.execution_id, "unmapped", reason)
            LOGGER.error(reason)
            return
        global_pause = self.store.global_pause_reason()
        if global_pause:
            self.store.update_event(
                event.execution_id, "paused", f"global pause: {global_pause}"
            )
            return
        if self.store.is_paused(event.symbol):
            self.store.update_event(event.execution_id, "paused", "symbol is paused")
            return
        if event.position_quantity is not None and event.position_quantity < 0:
            self._pause(event, "Alpaca fill would leave a short source position")
            return

        try:
            self._copy(event, mapping.target)
        except AmbiguousExecutionError as exc:
            self.store.complete_action(
                event.execution_id,
                status="ambiguous",
                executed_volume=Decimal("0"),
                detail=str(exc),
            )
            self._pause(event, str(exc))
        except SafetyError as exc:
            action = self.store.action(event.execution_id)
            if action:
                self.store.complete_action(
                    event.execution_id,
                    status="rejected",
                    executed_volume=Decimal("0"),
                    detail=str(exc),
                )
            self._pause(event, str(exc))

    def _pause(self, event: TradeEvent, reason: str) -> None:
        self.store.pause(event.symbol, reason)
        self.store.update_event(event.execution_id, "paused", reason)
        LOGGER.error("paused %s: %s", event.symbol, reason)

    def _validate_price(self, event: TradeEvent, target_symbol: str, side: str) -> Decimal:
        target_price = self.mt5.current_price(target_symbol, side)
        deviation = price_deviation_pct(event.price, target_price)
        allowed = Decimal(str(self.settings.max_price_deviation_pct))
        if deviation > allowed:
            raise SafetyError(
                f"price deviation {deviation:.4f}% exceeds {allowed}% for "
                f"{event.symbol}->{target_symbol}"
            )
        return target_price

    def _copy(self, event: TradeEvent, target_symbol: str) -> None:
        source_qty, target_volume, residual = self.store.allocation(
            event.symbol, target_symbol
        )
        spec = self.mt5.symbol_spec(target_symbol)
        if spec.currency_profit.upper() != "USD":
            raise SafetyError(
                f"{target_symbol} profit currency is {spec.currency_profit}, expected USD"
            )

        if event.side == "buy":
            target_price = self._validate_price(event, target_symbol, "buy")
            source_account = self.alpaca.account()
            target_account = self.mt5.account()
            volume, new_residual = buy_volume(
                fill_quantity=event.quantity,
                fill_price=event.price,
                source_equity=source_account.equity,
                target_equity=target_account.equity,
                target_price=target_price,
                spec=spec,
                residual_lots=residual,
            )
            new_source_qty = source_qty + event.quantity
            if volume == 0:
                self.store.commit_copy_state(
                    execution_id=event.execution_id,
                    source_symbol=event.symbol,
                    target_symbol=target_symbol,
                    source_quantity=new_source_qty,
                    target_volume=target_volume,
                    residual_lots=new_residual,
                    event_status="residual",
                    action_status="residual",
                    requested_volume=Decimal("0"),
                    executed_volume=Decimal("0"),
                )
                return
            correlation = self.store.create_action(
                event.execution_id, target_symbol, volume
            )
            existing = self.store.action(event.execution_id)
            if existing and existing["status"] in {"sending", "ambiguous"}:
                if self.mt5.find_correlation(correlation):
                    raise AmbiguousExecutionError(
                        f"existing MT5 deal found for unresolved {correlation}; reconcile before retry"
                    )
            self.store.complete_action(
                event.execution_id,
                status="sending",
                executed_volume=Decimal("0"),
            )
            result = self.mt5.open_long(target_symbol, volume, correlation)
            final_source = new_source_qty
            final_target = target_volume + result.volume
            final_residual = new_residual
        else:
            close_volume, remaining_source, remaining_target, remaining_residual = sell_volume(
                fill_quantity=event.quantity,
                managed_source_quantity=source_qty,
                managed_target_volume=target_volume,
                residual_lots=residual,
                spec=spec,
            )
            if close_volume == 0:
                self.store.commit_copy_state(
                    execution_id=event.execution_id,
                    source_symbol=event.symbol,
                    target_symbol=target_symbol,
                    source_quantity=remaining_source,
                    target_volume=remaining_target,
                    residual_lots=remaining_residual,
                    event_status="long_only_skip",
                    action_status="long_only_skip",
                    requested_volume=Decimal("0"),
                    executed_volume=Decimal("0"),
                )
                return
            self._validate_price(event, target_symbol, "sell")
            correlation = self.store.create_action(
                event.execution_id, target_symbol, close_volume
            )
            self.store.complete_action(
                event.execution_id,
                status="sending",
                executed_volume=Decimal("0"),
            )
            result = self.mt5.close_long(target_symbol, close_volume, correlation)
            final_source = remaining_source
            final_target = max(Decimal("0"), target_volume - result.volume)
            final_residual = remaining_residual

        self.store.commit_copy_state(
            execution_id=event.execution_id,
            source_symbol=event.symbol,
            target_symbol=target_symbol,
            source_quantity=final_source,
            target_volume=final_target,
            residual_lots=final_residual,
            event_status="confirmed",
            action_status="confirmed",
            requested_volume=volume if event.side == "buy" else close_volume,
            executed_volume=result.volume,
            order_ticket=result.order_ticket,
            deal_ticket=result.deal_ticket,
            retcode=result.retcode,
            detail=result.comment,
        )
        LOGGER.info(
            "confirmed %s %s %s -> %s lots on %s",
            event.side,
            event.quantity,
            event.symbol,
            result.volume,
            target_symbol,
        )


def _positions_by_symbol(positions: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for position in positions:
        result.setdefault(str(position["symbol"]), []).append(position)
    return result


def _long_volumes(
    positions: list[dict[str, Any]], buy_position_type: int
) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for position in positions:
        if int(position["type"]) == int(buy_position_type):
            symbol = str(position["symbol"])
            result[symbol] = result.get(symbol, Decimal("0")) + position["volume"]
    return result


def preflight(
    settings: Settings,
    store: StateStore,
    alpaca: AlpacaGateway,
    mt5: Mt5Gateway,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    if not settings.alpaca_paper:
        add("alpaca_mode", False, "live Alpaca mode is forbidden in version 1")
    else:
        add("alpaca_mode", True, "paper")
    add(
        "long_only_mode",
        settings.long_only,
        "enabled" if settings.long_only else "version 1 forbids short copying",
    )
    source_account = alpaca.account()
    add("alpaca_account", source_account.equity > 0, f"id={source_account.account_id} equity={source_account.equity}")
    target_account = mt5.account()
    add("mt5_account", target_account.equity > 0, f"login={target_account.account_id} equity={target_account.equity}")
    add(
        "mt5_demo_mode",
        (not settings.require_demo) or target_account.is_demo,
        "demo" if target_account.is_demo else "not demo",
    )
    terminal = mt5.terminal_status()
    add("mt5_connected", terminal["connected"], terminal["terminal_path"])
    add("mt5_trading_allowed", terminal["trade_allowed"], terminal["terminal_name"])

    enabled = settings.enabled_mappings
    add(
        "symbol_universe",
        bool(enabled) and bool(settings.catalog_hash),
        f"count={len(enabled)} stocks={settings.catalog_counts.get('stocks', 0)} "
        f"etfs={settings.catalog_counts.get('etfs', 0)} hash={settings.catalog_hash}",
    )
    source_positions = alpaca.positions()
    target_positions = mt5.positions()
    positions_by_symbol = _positions_by_symbol(target_positions)
    target_longs = _long_volumes(target_positions, mt5.buy_position_type)
    specs = mt5.symbol_specs()
    has_history = store.has_history()
    mapping_failures = 0
    source_mismatches = 0
    target_mismatches = 0
    foreign_count = 0
    for source, mapping in enabled.items():
        try:
            spec = specs[mapping.target]
            if spec.currency_profit.upper() != "USD" or spec.trade_mode != mt5.full_trade_mode:
                raise SafetyError(
                    f"{mapping.target} is not full-trade USD (mode={spec.trade_mode}, "
                    f"currency={spec.currency_profit})"
                )
        except Exception as exc:
            mapping_failures += 1
            add(f"mapping:{source}", False, str(exc))
        if not has_history:
            source_qty = source_positions.get(source, Decimal("0"))
            target_qty = target_longs.get(mapping.target, Decimal("0"))
            if source_qty != 0:
                source_mismatches += 1
                add(f"initial_flat_alpaca:{source}", False, f"quantity={source_qty}")
            if target_qty != 0:
                target_mismatches += 1
                add(f"initial_flat_mt5:{source}", False, f"volume={target_qty}")
        else:
            managed_source, managed_target, _ = store.allocation(source, mapping.target)
            live_source = source_positions.get(source, Decimal("0"))
            live_target = target_longs.get(mapping.target, Decimal("0"))
            if live_source != managed_source:
                source_mismatches += 1
                add(
                    f"state_match_alpaca:{source}",
                    False,
                    f"live={live_source} managed={managed_source}",
                )
            if live_target != managed_target:
                target_mismatches += 1
                add(
                    f"state_match_mt5:{source}",
                    False,
                    f"live={live_target} managed={managed_target}",
                )
        foreign_positions = [
            position
            for position in positions_by_symbol.get(mapping.target, [])
            if position["volume"] > 0 and position["magic"] != settings.magic
        ]
        if foreign_positions:
            foreign_count += len(foreign_positions)
            add(
                f"reserved_mt5_symbol:{source}",
                False,
                f"foreign_positions={len(foreign_positions)}",
            )

    add("mapping_validation", mapping_failures == 0, f"failures={mapping_failures}")
    add("alpaca_position_match", source_mismatches == 0, f"failures={source_mismatches}")
    add("mt5_position_match", target_mismatches == 0, f"failures={target_mismatches}")
    add("reserved_mt5_symbols", foreign_count == 0, f"foreign_positions={foreign_count}")
    global_pause = store.global_pause_reason()
    add("global_pause", global_pause is None, global_pause or "none")
    paused = store.paused_symbols()
    add("paused_symbols", not paused, f"count={len(paused)}")

    unresolved = store.unresolved_actions()
    add("unresolved_actions", not unresolved, f"count={len(unresolved)}")
    return {"ok": all(check["ok"] for check in checks), "checks": checks}


def reconciliation_snapshot(
    settings: Settings,
    store: StateStore,
    alpaca: AlpacaGateway,
    mt5: Mt5Gateway,
) -> dict[str, Any]:
    source_positions = alpaca.positions()
    source_account = alpaca.account()
    target_account = mt5.account()
    target_positions = mt5.positions()
    target_longs = _long_volumes(target_positions, mt5.buy_position_type)
    mapped = {}
    for source, mapping in settings.enabled_mappings.items():
        managed_source, managed_target, residual = store.allocation(source, mapping.target)
        mapped[source] = {
            "target": mapping.target,
            "alpaca_position": str(source_positions.get(source, Decimal("0"))),
            "managed_source_quantity": str(managed_source),
            "managed_target_volume": str(managed_target),
            "residual_lots": str(residual),
            "mt5_long_volume": str(target_longs.get(mapping.target, Decimal("0"))),
            "paused": store.is_paused(source),
        }
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "alpaca_account": {
            **asdict(source_account),
            "equity": str(source_account.equity),
        },
        "mt5_account": {
            **asdict(target_account),
            "equity": str(target_account.equity),
        },
        "symbols": mapped,
        "unresolved_actions": store.unresolved_actions(),
    }


def make_reconciliation_plan(
    settings: Settings,
    store: StateStore,
    alpaca: AlpacaGateway,
    mt5: Mt5Gateway,
) -> tuple[str, dict[str, Any]]:
    snapshot = reconciliation_snapshot(settings, store, alpaca, mt5)
    expiry = datetime.now(timezone.utc) + timedelta(
        seconds=settings.reconciliation_plan_ttl_seconds
    )
    plan_id = store.save_reconciliation_plan(snapshot, expires_at=expiry.isoformat())
    return plan_id, snapshot


def apply_reconciliation_plan(
    settings: Settings,
    store: StateStore,
    alpaca: AlpacaGateway,
    mt5: Mt5Gateway,
    plan_id: str,
) -> dict[str, Any]:
    row = store.reconciliation_plan(plan_id)
    if row is None:
        raise SafetyError(f"reconciliation plan not found: {plan_id}")
    if row["status"] != "pending":
        raise SafetyError(f"reconciliation plan is {row['status']}")
    if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
        store.mark_plan(plan_id, "expired")
        raise SafetyError("reconciliation plan expired")
    current = reconciliation_snapshot(settings, store, alpaca, mt5)
    previous = json.loads(row["snapshot_json"])
    comparable_previous = dict(previous)
    comparable_current = dict(current)
    comparable_previous.pop("created_at", None)
    comparable_current.pop("created_at", None)
    comparable_previous.get("alpaca_account", {}).pop("equity", None)
    comparable_current.get("alpaca_account", {}).pop("equity", None)
    comparable_previous.get("mt5_account", {}).pop("equity", None)
    comparable_current.get("mt5_account", {}).pop("equity", None)
    if comparable_previous != comparable_current:
        raise SafetyError("live state changed; generate a new reconciliation plan")

    for source, details in current["symbols"].items():
        if (
            details["alpaca_position"] != details["managed_source_quantity"]
            or details["mt5_long_volume"] != details["managed_target_volume"]
        ):
            raise SafetyError(
                "version 1 will not automatically trade an unresolved mismatch; "
                "flatten both mapped accounts and reset state under separate operator review"
            )
    for action in current["unresolved_actions"]:
        store.resolve_action_without_retry(
            action["execution_id"],
            f"operator applied reconciliation plan {plan_id}; no retry",
        )
    unpaused: list[str] = []
    for paused in store.paused_symbols():
        source = str(paused["source_symbol"])
        if source in current["symbols"]:
            store.unpause(source)
            unpaused.append(source)
    store.clear_global_pause()
    store.mark_plan(plan_id, "applied")
    return {"plan_id": plan_id, "status": "applied", "symbols_unpaused": unpaused}
