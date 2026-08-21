import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from copytrader.config import Settings
from copytrader.engine import CopyEngine, preflight
from copytrader.models import AccountSnapshot, ExecutionResult, SymbolMapping, SymbolSpec
from copytrader.storage import StateStore


class FakeAlpaca:
    def __init__(self):
        self._positions = {}

    def account(self):
        return AccountSnapshot(Decimal("100000"), "paper-1", True)

    def positions(self):
        return dict(self._positions)


class FakeMt5:
    def __init__(self):
        self.volume = Decimal("0")
        self.opens = []
        self.closes = []

    def account(self):
        return AccountSnapshot(Decimal("50000"), "demo-1", True)

    def terminal_status(self):
        return {
            "connected": True,
            "trade_allowed": True,
            "terminal_name": "Fake MT5",
            "terminal_path": "terminal64.exe",
        }

    def symbol_spec(self, symbol):
        return SymbolSpec(
            symbol,
            Decimal("1"),
            Decimal("0.1"),
            Decimal("100"),
            Decimal("0.1"),
            Decimal("0.01"),
            "USD",
            4,
        )

    def symbol_specs(self):
        return {"AAPL.US": self.symbol_spec("AAPL.US")}

    @property
    def full_trade_mode(self):
        return 4

    @property
    def buy_position_type(self):
        return 0

    def current_price(self, symbol, side):
        return Decimal("100")

    def long_volume(self, symbol):
        return self.volume

    def positions(self, symbol=None):
        if self.volume == 0:
            return []
        return [
            {
                "ticket": 1,
                "symbol": symbol or "AAPL.US",
                "type": 0,
                "volume": self.volume,
                "price_open": Decimal("100"),
                "magic": 926701,
                "comment": "cp-test",
            }
        ]

    def open_long(self, symbol, volume, correlation, *, price=None):
        self.volume += volume
        self.opens.append((symbol, volume, correlation, price))
        return ExecutionResult(True, volume, Decimal("100"), "10", "20", 10009, "done")

    def close_long(self, symbol, volume, correlation, *, price=None):
        actual = min(volume, self.volume)
        self.volume -= actual
        self.closes.append((symbol, actual, correlation))
        return ExecutionResult(True, actual, Decimal("100"), "11", "21", 10009, "done")

    def find_correlation(self, correlation):
        return False


def settings(root: Path) -> Settings:
    return Settings(
        project_root=root,
        alpaca_key="key",
        alpaca_secret="secret",
        alpaca_paper=True,
        mt5_path=Path("terminal64.exe"),
        mt5_portable=False,
        mt5_login=None,
        mt5_password=None,
        mt5_server=None,
        require_demo=True,
        magic=926701,
        long_only=True,
        max_price_deviation_pct=0.5,
        quote_acquisition_timeout_seconds=5,
        poll_interval_seconds=15,
        reconciliation_plan_ttl_seconds=300,
        database_path=root / "state.db",
        log_directory=root / "logs",
        snapshot_path=root / "symbols.json",
        stock_path_prefix="Stocks\\US\\",
        etf_path_prefix="ETFs\\",
        symbol_aliases={"BRK.B": "BRKb"},
        catalog_hash="catalog-hash",
        catalog_generated_at="2026-08-18T00:00:00+00:00",
        catalog_counts={"stocks": 1, "etfs": 0, "mappings": 1, "excluded": 0},
        mappings=(SymbolMapping("AAPL", "AAPL.US", "stock"),),
    )


def update(execution_id, side, qty="2", position="2"):
    return {
        "event": "fill",
        "execution_id": execution_id,
        "qty": qty,
        "price": "100",
        "position_qty": position,
        "order": {"id": f"order-{execution_id}", "symbol": "AAPL", "side": side},
    }


class EngineTests(unittest.TestCase):
    def test_buy_deduplicates_and_sell_closes_without_short(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = settings(root)
            store = StateStore(config.database_path)
            alpaca = FakeAlpaca()
            mt5 = FakeMt5()
            engine = CopyEngine(config, store, alpaca, mt5)

            engine.process(update("buy-1", "buy"))
            engine.process(update("buy-1", "buy"))
            self.assertEqual(mt5.volume, Decimal("1.0"))
            self.assertEqual(len(mt5.opens), 1)

            engine.process(update("sell-1", "sell", qty="99", position="0"))
            self.assertEqual(mt5.volume, Decimal("0"))
            self.assertEqual(len(mt5.closes), 1)
            self.assertEqual(store.event_status("sell-1"), "confirmed")

    def test_unmanaged_sell_never_reaches_mt5(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = settings(root)
            store = StateStore(config.database_path)
            mt5 = FakeMt5()
            engine = CopyEngine(config, store, FakeAlpaca(), mt5)
            engine.process(update("sell-short", "sell", qty="1", position="0"))
            self.assertEqual(mt5.closes, [])
            self.assertEqual(store.event_status("sell-short"), "long_only_skip")

    def test_engine_passes_single_validated_quote_to_mt5_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = settings(root)
            store = StateStore(config.database_path)
            mt5 = FakeMt5()
            engine = CopyEngine(config, store, FakeAlpaca(), mt5)

            engine.process(update("buy-price", "buy"))

            self.assertEqual(mt5.opens[0][3], Decimal("100"))

    def test_preflight_requires_flat_initial_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = settings(root)
            store = StateStore(config.database_path)
            alpaca = FakeAlpaca()
            alpaca._positions["AAPL"] = Decimal("1")
            report = preflight(config, store, alpaca, FakeMt5())
            self.assertFalse(report["ok"])
            failed = {check["name"] for check in report["checks"] if not check["ok"]}
            self.assertIn("initial_flat_alpaca:AAPL", failed)

    def test_preflight_uses_bulk_mt5_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = settings(root)
            store = StateStore(config.database_path)
            report = preflight(config, store, FakeAlpaca(), FakeMt5())
            self.assertTrue(report["ok"])
            self.assertIn(
                "symbol_universe", {check["name"] for check in report["checks"]}
            )

    def test_global_pause_blocks_event_before_mt5(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = settings(root)
            store = StateStore(config.database_path)
            store.pause_all("MT5 IPC unavailable")
            mt5 = FakeMt5()
            engine = CopyEngine(config, store, FakeAlpaca(), mt5)
            engine.process(update("paused-buy", "buy"))
            self.assertEqual(mt5.opens, [])
            self.assertEqual(store.event_status("paused-buy"), "paused")

    def test_persistent_drift_pauses_on_second_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = settings(root)
            store = StateStore(config.database_path)
            alpaca = FakeAlpaca()
            alpaca._positions["AAPL"] = Decimal("1")
            engine = CopyEngine(config, store, alpaca, FakeMt5())
            engine._check_drift_once()
            self.assertFalse(store.is_paused("AAPL"))
            engine._check_drift_once()
            self.assertTrue(store.is_paused("AAPL"))


if __name__ == "__main__":
    unittest.main()
