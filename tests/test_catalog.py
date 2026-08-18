import json
import tempfile
import unittest
from pathlib import Path

from copytrader.catalog import (
    build_snapshot,
    load_snapshot,
    snapshot_diff,
    write_snapshot_atomic,
)
from copytrader.models import ConfigurationError


def symbol(name, path, *, trade_mode=4, currency="USD"):
    return {
        "name": name,
        "description": name,
        "path": path,
        "currency_profit": currency,
        "trade_mode": trade_mode,
    }


class CatalogTests(unittest.TestCase):
    def build(self):
        return build_snapshot(
            [
                symbol("AAPL", r"Stocks\US\Nasdaq\AAPL"),
                symbol("BRKb", r"Stocks\US\NYSE\BRKb"),
                symbol("OLD", r"Stocks\US\NYSE\OLD"),
                symbol("CLOSE", r"Stocks\US\NYSE\CLOSE", trade_mode=3),
                symbol("SPY", r"ETFs\SPY"),
                symbol("VOE", r"ETFs\VOE", trade_mode=1),
                symbol("EURUSD", r"Forex\EURUSD", currency="EUR"),
            ],
            {"AAPL", "BRK.B", "SPY"},
            stock_path_prefix="Stocks\\US\\",
            etf_path_prefix="ETFs\\",
            aliases={"BRK.B": "BRKb"},
            full_trade_mode=4,
        )

    def test_builds_stocks_etfs_aliases_and_exclusions(self):
        snapshot = self.build()
        self.assertEqual(snapshot["counts"]["stocks"], 2)
        self.assertEqual(snapshot["counts"]["etfs"], 1)
        self.assertEqual(snapshot["counts"]["mappings"], 3)
        self.assertEqual(
            [(item["source"], item["target"]) for item in snapshot["mappings"]],
            [("AAPL", "AAPL"), ("BRK.B", "BRKb"), ("SPY", "SPY")],
        )
        reasons = {item["target"]: item["reason"] for item in snapshot["excluded"]}
        self.assertEqual(reasons["CLOSE"], "mt5_not_full_trade")
        self.assertEqual(reasons["OLD"], "not_active_tradable_on_alpaca")
        self.assertEqual(reasons["VOE"], "mt5_not_full_trade")

    def test_atomic_round_trip_and_hash_validation(self):
        snapshot = self.build()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "symbols.json"
            write_snapshot_atomic(path, snapshot)
            mappings, loaded = load_snapshot(
                path,
                stock_path_prefix="Stocks\\US\\",
                etf_path_prefix="ETFs\\",
                aliases={"BRK.B": "BRKb"},
            )
            self.assertEqual(len(mappings), 3)
            self.assertEqual(loaded["catalog_hash"], snapshot["catalog_hash"])

            tampered = json.loads(path.read_text(encoding="utf-8"))
            tampered["mappings"][0]["target"] = "WRONG"
            path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "hash validation"):
                load_snapshot(
                    path,
                    stock_path_prefix="Stocks\\US\\",
                    etf_path_prefix="ETFs\\",
                    aliases={"BRK.B": "BRKb"},
                )

    def test_diff_is_stable_for_same_catalog(self):
        snapshot = self.build()
        self.assertFalse(snapshot_diff(snapshot, self.build())["changed"])

    def test_rejects_empty_catalog(self):
        with self.assertRaisesRegex(ConfigurationError, "no eligible mappings"):
            build_snapshot(
                [],
                set(),
                stock_path_prefix="Stocks\\US\\",
                etf_path_prefix="ETFs\\",
                aliases={},
                full_trade_mode=4,
            )


if __name__ == "__main__":
    unittest.main()
