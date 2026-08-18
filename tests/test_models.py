import unittest

from copytrader.models import SafetyError, TradeEvent


class TradeEventTests(unittest.TestCase):
    def test_parses_fill(self):
        event = TradeEvent.from_update(
            {
                "event": "partial_fill",
                "execution_id": "exec-1",
                "qty": "2.5",
                "price": "100.25",
                "position_qty": "2.5",
                "order": {"id": "order-1", "symbol": "aapl", "side": "buy"},
            }
        )
        self.assertEqual(event.symbol, "AAPL")
        self.assertEqual(str(event.quantity), "2.5")

    def test_rejects_non_fill(self):
        with self.assertRaises(SafetyError):
            TradeEvent.from_update({"event": "accepted", "order": {}})


if __name__ == "__main__":
    unittest.main()

