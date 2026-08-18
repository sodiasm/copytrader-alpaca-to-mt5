import sys
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


class Mt5GatewayTests(unittest.TestCase):
    def test_connect_passes_exact_configured_terminal_path(self):
        terminal = Path(r"C:\Program Files\Darwinex MetaTrader 5 Demo1\terminal64.exe")
        settings = SimpleNamespace(
            mt5_path=terminal,
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
                        "login": 123456,
                        "password": "password",
                        "server": "Darwinex-Demo",
                    },
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
