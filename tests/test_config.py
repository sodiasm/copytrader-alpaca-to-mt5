import tempfile
import unittest
from pathlib import Path

from copytrader.config import load_settings
from copytrader.models import ConfigurationError


def toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_config(
    root: Path,
    terminal_path: str | None,
    *,
    portable: object | None = None,
    quote_timeout: object | None = None,
) -> Path:
    config_path = root / "config.toml"
    lines = ["[mt5]"]
    if terminal_path is not None:
        lines.append(f"terminal_path = {toml_string(terminal_path)}")
    if portable is not None:
        value = str(portable).lower() if isinstance(portable, bool) else str(portable)
        lines.append(f"portable = {value}")
    if quote_timeout is not None:
        lines.extend(["[copy]", f"quote_acquisition_timeout_seconds = {quote_timeout}"])
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
            self.assertFalse(settings.mt5_portable)

    def test_accepts_portable_mt5_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            terminal = root / "mt5" / "terminal64.exe"
            terminal.parent.mkdir()
            terminal.touch()

            settings = load_settings(
                write_config(root, str(terminal), portable=True),
                require_credentials=False,
                require_snapshot=False,
            )

            self.assertTrue(settings.mt5_portable)

    def test_rejects_non_boolean_portable_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            terminal = root / "terminal64.exe"
            terminal.touch()

            with self.assertRaisesRegex(
                ConfigurationError, "mt5\\.portable must be true or false"
            ):
                load_settings(
                    write_config(root, str(terminal), portable='"yes"'),
                    require_credentials=False,
                    require_snapshot=False,
                )

    def test_accepts_bounded_quote_acquisition_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            terminal = root / "terminal64.exe"
            terminal.touch()
            settings = load_settings(
                write_config(root, str(terminal), quote_timeout="5"),
                require_credentials=False,
                require_snapshot=False,
            )
            self.assertEqual(settings.quote_acquisition_timeout_seconds, 5)

    def test_rejects_out_of_range_quote_acquisition_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            terminal = root / "terminal64.exe"
            terminal.touch()
            with self.assertRaisesRegex(
                ConfigurationError,
                "copy\\.quote_acquisition_timeout_seconds must be in \\[0, 10\\]",
            ):
                load_settings(
                    write_config(root, str(terminal), quote_timeout="10.1"),
                    require_credentials=False,
                    require_snapshot=False,
                )

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
