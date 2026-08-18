import tempfile
import unittest
from pathlib import Path

from copytrader.config import load_settings
from copytrader.models import ConfigurationError


def toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_config(root: Path, terminal_path: str | None) -> Path:
    config_path = root / "config.toml"
    lines = ["[mt5]"]
    if terminal_path is not None:
        lines.append(f"terminal_path = {toml_string(terminal_path)}")
    lines.extend(
        [
            "[symbol_universe]",
            'snapshot_path = "symbols.json"',
            'stock_path_prefix = "Stocks\\\\US\\\\"',
            'etf_path_prefix = "ETFs\\\\"',
        ]
    )
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_path


class ConfigTests(unittest.TestCase):
    def test_accepts_absolute_terminal_path_with_spaces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            terminal = root / "Program Files" / "Darwinex MetaTrader 5" / "terminal64.exe"
            terminal.parent.mkdir(parents=True)
            terminal.touch()

            settings = load_settings(
                write_config(root, str(terminal)),
                require_credentials=False,
                require_snapshot=False,
            )

            self.assertEqual(settings.mt5_path, terminal)

    def test_resolves_relative_terminal_path_from_config_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            terminal = root / "mt5" / "terminal64.exe"
            terminal.parent.mkdir()
            terminal.touch()

            settings = load_settings(
                write_config(root, "mt5/terminal64.exe"),
                require_credentials=False,
                require_snapshot=False,
            )

            self.assertEqual(settings.mt5_path, terminal.resolve())

    def test_rejects_missing_or_blank_terminal_path(self):
        for terminal_path in (None, "", "   "):
            with self.subTest(terminal_path=terminal_path):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    with self.assertRaisesRegex(
                        ConfigurationError, "mt5\\.terminal_path is required"
                    ):
                        load_settings(
                            write_config(root, terminal_path),
                            require_credentials=False,
                            require_snapshot=False,
                        )

    def test_rejects_nonexistent_terminal_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = (root / "missing" / "terminal64.exe").resolve()

            with self.assertRaisesRegex(
                ConfigurationError,
                f"mt5\\.terminal_path does not exist: {str(expected).replace('\\', '\\\\')}",
            ):
                load_settings(
                    write_config(root, "missing/terminal64.exe"),
                    require_credentials=False,
                    require_snapshot=False,
                )

    def test_rejects_directory_terminal_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            terminal_directory = root / "mt5"
            terminal_directory.mkdir()

            with self.assertRaisesRegex(
                ConfigurationError, "mt5\\.terminal_path is not a file"
            ):
                load_settings(
                    write_config(root, str(terminal_directory)),
                    require_credentials=False,
                    require_snapshot=False,
                )

    def test_rejects_legacy_symbols_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            terminal = root / "terminal64.exe"
            terminal.touch()
            config = write_config(root, str(terminal))
            config.write_text(
                config.read_text(encoding="utf-8")
                + '\n[[symbols]]\nsource = "AAPL"\ntarget = "AAPL"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "legacy.*not supported"):
                load_settings(
                    config, require_credentials=False, require_snapshot=False
                )

    def test_run_configuration_fails_closed_when_snapshot_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            terminal = root / "terminal64.exe"
            terminal.touch()
            with self.assertRaisesRegex(
                ConfigurationError, "symbol catalog snapshot not found"
            ):
                load_settings(
                    write_config(root, str(terminal)), require_credentials=False
                )


if __name__ == "__main__":
    unittest.main()
