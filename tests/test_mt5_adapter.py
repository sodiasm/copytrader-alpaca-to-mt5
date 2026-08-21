import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from copytrader.mt5_adapter import Mt5Gateway


class FakeMetaTrader5:
    def __init__(self):
        self.initialize_calls = []

    def initialize(self, path, **kwargs):
        self.initialize_calls.append((path, kwargs))
        return True


class FakeQuoteMetaTrader5(FakeMetaTrader5):
    def __init__(self, ticks):
        super().__init__()
        self.ticks = list(ticks)

    def symbol_info_tick(self, symbol):
        return self.ticks.pop(0) if self.ticks else None

    def last_error(self):
        return (1, "Success")


class Mt5GatewayTests(unittest.TestCase):
    def test_connect_passes_exact_configured_terminal_path_and_portable_mode(self):
        terminal = Path(r"C:\Program Files\Darwinex MetaTrader 5 Demo1\terminal64.exe")
        settings = SimpleNamespace(
            mt5_path=terminal,
            mt5_portable=True,
            mt5_login=123456,
            mt5_password="password",
            mt5_server="Darwinex-Demo",
        )
        binding = FakeMetaTrader5()

        with patch.dict(sys.modules, {"MetaTrader5": binding}):
            gateway = Mt5Gateway(settings)
            gateway.connect()

        self.assertEqual(
            binding.initialize_calls,
            [
                (
                    str(terminal),
                    {
                        "portable": True,
                        "login": 123456,
                        "password": "password",
                        "server": "Darwinex-Demo",
                    },
                )
            ],
        )

    def _quote_gateway(self, ticks, timeout=0):
        binding = FakeQuoteMetaTrader5(ticks)
        settings = SimpleNamespace(quote_acquisition_timeout_seconds=timeout)
        with patch.dict(sys.modules, {"MetaTrader5": binding}):
            return Mt5Gateway(settings)

    def test_current_price_retries_zero_quote_until_fresh_quote(self):
        now_msc = int(time.time() * 1000)
        gateway = self._quote_gateway(
            [
                SimpleNamespace(ask=0, bid=0, time_msc=now_msc),
                SimpleNamespace(ask=100.25, bid=100, time_msc=now_msc),
            ],
            timeout=0.01,
        )

        self.assertEqual(str(gateway.current_price("AAPL", "buy")), "100.25")

    def test_current_price_rejects_unavailable_or_stale_quote(self):
        stale_msc = int((time.time() - 3) * 1000)
        gateway = self._quote_gateway(
            [SimpleNamespace(ask=100, bid=99.5, time_msc=stale_msc)]
        )

        with self.assertRaisesRegex(RuntimeError, "quote_unavailable"):
            gateway.current_price("AAPL", "buy")


if __name__ == "__main__":
    unittest.main()
