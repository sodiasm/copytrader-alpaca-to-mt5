import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from copytrader.models import TradeEvent
from copytrader.storage import StateStore


class StorageTests(unittest.TestCase):
    def test_event_deduplication_and_allocation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.db")
            event = TradeEvent.from_update(
                {
                    "event": "fill",
                    "execution_id": "same-id",
                    "qty": "1",
                    "price": "100",
                    "order": {"id": "o1", "symbol": "AAPL", "side": "buy"},
                }
            )
            self.assertTrue(store.record_event(event))
            self.assertFalse(store.record_event(event))
            store.set_allocation("AAPL", "AAPL.US", Decimal("1"), Decimal("0.5"), Decimal("0"))
            self.assertEqual(
                store.allocation("AAPL", "AAPL.US"),
                (Decimal("1"), Decimal("0.5"), Decimal("0")),
            )

    def test_global_pause_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.db")
            store.pause_all("MT5 unavailable")
            self.assertEqual(store.global_pause_reason(), "MT5 unavailable")
            self.assertEqual(store.status()["global_pause_reason"], "MT5 unavailable")
            store.clear_global_pause()
            self.assertIsNone(store.global_pause_reason())


if __name__ == "__main__":
    unittest.main()
