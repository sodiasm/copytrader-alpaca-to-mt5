from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from decimal import Decimal, ROUND_UP
from pathlib import Path
from typing import Any

from .alpaca_adapter import AlpacaGateway, AlpacaStream
from .catalog import (
    build_snapshot,
    read_snapshot_if_present,
    snapshot_diff,
    write_snapshot_atomic,
)
from .config import Settings, load_settings
from .engine import (
    CopyEngine,
    apply_reconciliation_plan,
    make_reconciliation_plan,
    preflight,
    reconciliation_snapshot,
)
from .logging_setup import configure_logging
from .models import ConfigurationError, CopyTraderError, SafetyError
from .mt5_adapter import Mt5Gateway
from .sizing import price_deviation_pct
from .storage import StateStore


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="copytrader",
        description="Long-only Alpaca paper to MT5 demo fill copier",
    )
    parser.add_argument("--config", default="config.toml", help="path to config.toml")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight", help="perform read-only account and mapping checks")
    sub.add_parser("status", help="show durable local state")
    sub.add_parser("run", help="run the fill copier")

    symbols = sub.add_parser("symbols", help="inspect MT5 symbol candidates")
    symbols_sub = symbols.add_subparsers(dest="symbols_command", required=True)
    discover = symbols_sub.add_parser("discover")
    discover.add_argument("query")
    sync = symbols_sub.add_parser(
        "sync-us", help="preview or write the reviewed U.S. stock and ETF catalog"
    )
    sync.add_argument("--write", action="store_true", help="atomically write the snapshot")

    reconcile = sub.add_parser("reconcile", help="plan or approve recovery")
    reconcile_sub = reconcile.add_subparsers(dest="reconcile_command", required=True)
    reconcile_sub.add_parser("plan")
    apply_parser = reconcile_sub.add_parser("apply")
    apply_parser.add_argument("plan_id")
    apply_parser.add_argument("--yes", action="store_true")

    smoke = sub.add_parser("smoke-test", help="automated paper/demo round trip")
    smoke.add_argument("--symbol", default="AAPL")
    smoke.add_argument("--max-source-notional", type=Decimal, default=Decimal("1000"))
    smoke.add_argument("--timeout", type=int, default=120)
    smoke.add_argument("--yes", action="store_true")
    return parser


def load_runtime(
    config_path: Path,
    *,
    require_credentials: bool = True,
    require_snapshot: bool = True,
):
    settings = load_settings(
        config_path,
        require_credentials=require_credentials,
        require_snapshot=require_snapshot,
    )
    configure_logging(settings.log_directory)
    store = StateStore(settings.database_path)
    return settings, store


def connected_gateways(settings: Settings):
    alpaca = AlpacaGateway(settings)
    mt5 = Mt5Gateway(settings)
    mt5.connect()
    return alpaca, mt5


def wait_for_order(store: StateStore, order_id: str, timeout: int) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = store.order_events(order_id)
        if events and all(event["status"] in {"confirmed", "residual", "long_only_skip"} for event in events):
            return events
        if any(event["status"] in {"paused", "unmapped"} for event in events):
            raise SafetyError(f"copy paused for Alpaca order {order_id}: {events}")
        time.sleep(0.5)
    raise SafetyError(f"timed out waiting for Alpaca order {order_id}")


def run_stream(settings: Settings, store: StateStore, alpaca: AlpacaGateway, mt5: Mt5Gateway) -> None:
    report = preflight(settings, store, alpaca, mt5)
    if not report["ok"]:
        emit(report)
        raise SafetyError("preflight failed; copier not started")
    store.set_state("active_universe_hash", settings.catalog_hash or "")
    store.set_state(
        "active_universe_counts",
        json.dumps(settings.catalog_counts, sort_keys=True, separators=(",", ":")),
    )
    engine = CopyEngine(settings, store, alpaca, mt5)
    stream = AlpacaStream(settings)
    engine.start()
    try:
        stream.run(engine.enqueue)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        store.set_state("stream_state", "disconnected")
        store.pause_all(f"Alpaca stream disconnected: {type(exc).__name__}")
        raise
    finally:
        engine.stop()


def smoke_test(
    settings: Settings,
    store: StateStore,
    alpaca: AlpacaGateway,
    mt5: Mt5Gateway,
    *,
    symbol: str,
    max_notional: Decimal,
    timeout: int,
) -> dict[str, Any]:
    symbol = symbol.upper()
    mapping = settings.enabled_mappings.get(symbol)
    if mapping is None:
        raise SafetyError(f"{symbol} is not present in the reviewed symbol catalog")
    report = preflight(settings, store, alpaca, mt5)
    if not report["ok"]:
        raise SafetyError(f"preflight failed: {json.dumps(report, default=str)}")
    if not alpaca.market_is_open():
        raise SafetyError("Alpaca stock market is closed; smoke test did not place an order")

    source_price = alpaca.latest_trade_price(symbol)
    target_price = mt5.current_price(mapping.target, "buy")
    deviation = price_deviation_pct(source_price, target_price)
    if deviation > Decimal(str(settings.max_price_deviation_pct)):
        raise SafetyError(f"price deviation is {deviation:.4f}%; smoke test blocked")
    source_account = alpaca.account()
    target_account = mt5.account()
    spec = mt5.symbol_spec(mapping.target)
    equity_ratio = target_account.equity / source_account.equity
    raw_qty = spec.volume_min * target_price * spec.contract_size / (source_price * equity_ratio)
    quantity = raw_qty.quantize(Decimal("0.001"), rounding=ROUND_UP)
    notional = quantity * source_price
    if notional > max_notional:
        raise SafetyError(
            f"minimum viable order is approximately ${notional:.2f}, above ${max_notional} cap"
        )

    engine = CopyEngine(settings, store, alpaca, mt5)
    stream = AlpacaStream(settings)
    engine.start()
    stream_thread = threading.Thread(target=stream.run, args=(engine.enqueue,), daemon=True)
    stream_thread.start()
    buy_order = ""
    sell_order = ""
    buy_events: list[dict[str, Any]] = []
    sell_events: list[dict[str, Any]] = []
    try:
        stream.wait_until_ready(timeout=min(timeout, 30))
        buy_order = alpaca.submit_market_order(symbol, quantity, "buy")
        buy_events = wait_for_order(store, buy_order, timeout)
        sell_order = alpaca.close_position(symbol)
        sell_events = wait_for_order(store, sell_order, timeout)
    except Exception:
        if buy_order and not sell_order:
            try:
                if alpaca.positions().get(symbol, Decimal("0")) > 0:
                    sell_order = alpaca.close_position(symbol)
                    sell_events = wait_for_order(store, sell_order, min(timeout, 30))
            except Exception:
                pass
        raise
    finally:
        stream.stop()
        engine.stop()
    final_source = alpaca.positions().get(symbol, Decimal("0"))
    final_target = mt5.long_volume(mapping.target)
    if final_source != 0 or final_target != 0:
        raise SafetyError(
            f"smoke cleanup not flat: Alpaca={final_source}, MT5={final_target}"
        )
    return {
        "status": "passed",
        "symbol": symbol,
        "target_symbol": mapping.target,
        "source_quantity": str(quantity),
        "estimated_source_notional": str(notional),
        "buy_order": buy_order,
        "buy_events": buy_events,
        "sell_order": sell_order,
        "sell_events": sell_events,
        "final_alpaca_quantity": str(final_source),
        "final_mt5_volume": str(final_target),
    }


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config)
    try:
        if args.command == "status":
            base_settings, _ = load_runtime(
                config_path, require_credentials=False, require_snapshot=False
            )
            settings, store = (
                load_runtime(config_path, require_credentials=False)
                if base_settings.snapshot_path.exists()
                else (base_settings, StateStore(base_settings.database_path))
            )
            status = store.status()
            active_counts = status.get("active_universe_counts")
            if active_counts:
                try:
                    active_counts = json.loads(active_counts)
                except json.JSONDecodeError:
                    active_counts = {"invalid": active_counts}
            status["symbol_universe"] = {
                "snapshot_path": str(settings.snapshot_path),
                "snapshot_exists": settings.snapshot_path.exists(),
                "stored_hash": settings.catalog_hash,
                "active_process_hash": status.pop("active_universe_hash", None),
                "stored_counts": settings.catalog_counts,
                "active_process_counts": active_counts,
            }
            stored_hash = status["symbol_universe"]["stored_hash"]
            active_hash = status["symbol_universe"]["active_process_hash"]
            status["symbol_universe"]["restart_required"] = bool(
                stored_hash and active_hash and stored_hash != active_hash
            )
            status.pop("active_universe_counts", None)
            emit(status)
            return

        if args.command == "symbols" and args.symbols_command == "discover":
            settings, _ = load_runtime(
                config_path, require_credentials=False, require_snapshot=False
            )
            mt5 = Mt5Gateway(settings)
            mt5.connect()
            try:
                emit({"query": args.query, "matches": mt5.discover_symbols(args.query)})
            finally:
                mt5.shutdown()
            return

        if args.command == "symbols" and args.symbols_command == "sync-us":
            settings, store = load_runtime(config_path, require_snapshot=False)
            alpaca, mt5 = connected_gateways(settings)
            try:
                candidate = build_snapshot(
                    mt5.all_symbols(),
                    alpaca.active_tradable_us_assets(),
                    stock_path_prefix=settings.stock_path_prefix,
                    etf_path_prefix=settings.etf_path_prefix,
                    aliases=settings.symbol_aliases,
                    full_trade_mode=mt5.full_trade_mode,
                )
                previous = read_snapshot_if_present(settings.snapshot_path)
                diff = snapshot_diff(previous, candidate)
                same_content = bool(
                    previous
                    and previous.get("catalog_hash") == candidate.get("catalog_hash")
                )
                written = False
                if args.write and not same_content:
                    write_snapshot_atomic(settings.snapshot_path, candidate)
                    written = True
                active_hash = store.get_state("active_universe_hash")
                emit(
                    {
                        "mode": "write" if args.write else "preview",
                        "snapshot_path": str(settings.snapshot_path),
                        "catalog_hash": candidate["catalog_hash"],
                        "counts": candidate["counts"],
                        "diff": diff,
                        "excluded": candidate["excluded"],
                        "written": written,
                        "unchanged": same_content,
                        "active_process_hash": active_hash,
                        "restart_required": bool(
                            active_hash and active_hash != candidate["catalog_hash"]
                        ),
                    }
                )
            finally:
                mt5.shutdown()
            return

        settings, store = load_runtime(config_path)
        configure_logging(settings.log_directory, verbose=args.verbose)
        alpaca, mt5 = connected_gateways(settings)
        try:
            if args.command == "preflight":
                report = preflight(settings, store, alpaca, mt5)
                emit(report)
                if not report["ok"]:
                    raise SystemExit(2)
            elif args.command == "run":
                run_stream(settings, store, alpaca, mt5)
            elif args.command == "reconcile" and args.reconcile_command == "plan":
                plan_id, snapshot = make_reconciliation_plan(settings, store, alpaca, mt5)
                emit({"plan_id": plan_id, "snapshot": snapshot})
            elif args.command == "reconcile" and args.reconcile_command == "apply":
                if not args.yes:
                    raise SafetyError("reconcile apply requires --yes")
                emit(apply_reconciliation_plan(settings, store, alpaca, mt5, args.plan_id))
            elif args.command == "smoke-test":
                if not args.yes:
                    raise SafetyError("smoke-test places demo orders and requires --yes")
                emit(
                    smoke_test(
                        settings,
                        store,
                        alpaca,
                        mt5,
                        symbol=args.symbol,
                        max_notional=args.max_source_notional,
                        timeout=args.timeout,
                    )
                )
        finally:
            mt5.shutdown()
    except (CopyTraderError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
