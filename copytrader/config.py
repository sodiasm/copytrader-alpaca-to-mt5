from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from math import isfinite
from pathlib import Path

from .catalog import load_snapshot
from .models import ConfigurationError, SymbolMapping


@dataclass(frozen=True)
class Settings:
    project_root: Path
    alpaca_key: str
    alpaca_secret: str
    alpaca_paper: bool
    mt5_path: Path
    mt5_portable: bool
    mt5_login: int | None
    mt5_password: str | None
    mt5_server: str | None
    require_demo: bool
    magic: int
    long_only: bool
    max_price_deviation_pct: float
    quote_acquisition_timeout_seconds: float
    poll_interval_seconds: int
    reconciliation_plan_ttl_seconds: int
    database_path: Path
    log_directory: Path
    snapshot_path: Path
    stock_path_prefix: str
    etf_path_prefix: str
    symbol_aliases: dict[str, str]
    catalog_hash: str | None
    catalog_generated_at: str | None
    catalog_counts: dict[str, int]
    mappings: tuple[SymbolMapping, ...]

    @property
    def enabled_mappings(self) -> dict[str, SymbolMapping]:
        return {mapping.source: mapping for mapping in self.mappings}


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigurationError(f"{path}:{line_number}: expected NAME=value")
        name, value = line.split("=", 1)
        name = name.strip()
        if not name:
            raise ConfigurationError(f"{path}:{line_number}: empty variable name")
        os.environ.setdefault(name, value.strip().strip('"').strip("'"))


def _relative(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def load_settings(
    config_path: Path,
    *,
    require_credentials: bool = True,
    require_snapshot: bool = True,
) -> Settings:
    config_path = config_path.resolve()
    root = config_path.parent
    load_env_file(root / ".env")
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"configuration not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"invalid TOML: {exc}") from exc

    alpaca = data.get("alpaca", {})
    mt5 = data.get("mt5", {})
    copy = data.get("copy", {})
    if "symbols" in data:
        raise ConfigurationError(
            "legacy [[symbols]] configuration is not supported; use [symbol_universe]"
        )
    universe = data.get("symbol_universe")
    if not isinstance(universe, dict):
        raise ConfigurationError("[symbol_universe] configuration is required")

    key = os.getenv("ALPACA_API_KEY", "").strip()
    secret = os.getenv("ALPACA_SECRET_KEY", "").strip()
    if require_credentials and (not key or not secret):
        raise ConfigurationError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required in .env")

    raw_mt5_path = mt5.get("terminal_path")
    if not isinstance(raw_mt5_path, str) or not raw_mt5_path.strip():
        raise ConfigurationError("mt5.terminal_path is required")
    configured_mt5_path = Path(raw_mt5_path.strip())
    mt5_path = (
        configured_mt5_path
        if configured_mt5_path.is_absolute()
        else (root / configured_mt5_path).resolve()
    )
    if not mt5_path.exists():
        raise ConfigurationError(f"mt5.terminal_path does not exist: {mt5_path}")
    if not mt5_path.is_file():
        raise ConfigurationError(f"mt5.terminal_path is not a file: {mt5_path}")
    portable = mt5.get("portable", False)
    if not isinstance(portable, bool):
        raise ConfigurationError("mt5.portable must be true or false")
    login_text = os.getenv("MT5_LOGIN", "").strip()
    snapshot_value = universe.get("snapshot_path")
    if not isinstance(snapshot_value, str) or not snapshot_value.strip():
        raise ConfigurationError("symbol_universe.snapshot_path is required")
    snapshot_path = _relative(root, snapshot_value.strip())
    stock_path_prefix = str(universe.get("stock_path_prefix") or "").strip()
    etf_path_prefix = str(universe.get("etf_path_prefix") or "").strip()
    if not stock_path_prefix or not etf_path_prefix:
        raise ConfigurationError(
            "symbol_universe stock_path_prefix and etf_path_prefix are required"
        )
    if stock_path_prefix == etf_path_prefix:
        raise ConfigurationError("stock and ETF path prefixes must differ")
    raw_aliases = universe.get("aliases", {})
    if not isinstance(raw_aliases, dict):
        raise ConfigurationError("symbol_universe.aliases must be a table")
    symbol_aliases = {
        str(source).strip().upper(): str(target).strip()
        for source, target in raw_aliases.items()
    }
    if any(not source or not target for source, target in symbol_aliases.items()):
        raise ConfigurationError("symbol universe aliases cannot be blank")
    mappings: tuple[SymbolMapping, ...] = ()
    snapshot: dict[str, object] = {}
    if require_snapshot:
        mappings, snapshot = load_snapshot(
            snapshot_path,
            stock_path_prefix=stock_path_prefix,
            etf_path_prefix=etf_path_prefix,
            aliases=symbol_aliases,
        )

    deviation = float(copy.get("max_price_deviation_pct", 0.5))
    if deviation <= 0 or deviation > 10:
        raise ConfigurationError("copy.max_price_deviation_pct must be in (0, 10]")
    try:
        quote_timeout = float(copy.get("quote_acquisition_timeout_seconds", 5))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(
            "copy.quote_acquisition_timeout_seconds must be a number"
        ) from exc
    if not isfinite(quote_timeout) or not 0 <= quote_timeout <= 10:
        raise ConfigurationError(
            "copy.quote_acquisition_timeout_seconds must be in [0, 10]"
        )
    magic = int(mt5.get("magic", 926701))
    if magic <= 0:
        raise ConfigurationError("mt5.magic must be positive")

    return Settings(
        project_root=root,
        alpaca_key=key,
        alpaca_secret=secret,
        alpaca_paper=bool(alpaca.get("paper", True)),
        mt5_path=mt5_path,
        mt5_portable=portable,
        mt5_login=int(login_text) if login_text else None,
        mt5_password=os.getenv("MT5_PASSWORD") or None,
        mt5_server=os.getenv("MT5_SERVER") or None,
        require_demo=bool(mt5.get("require_demo", True)),
        magic=magic,
        long_only=bool(copy.get("long_only", True)),
        max_price_deviation_pct=deviation,
        quote_acquisition_timeout_seconds=quote_timeout,
        poll_interval_seconds=max(5, int(copy.get("poll_interval_seconds", 15))),
        reconciliation_plan_ttl_seconds=max(
            60, int(copy.get("reconciliation_plan_ttl_seconds", 300))
        ),
        database_path=_relative(root, str(copy.get("database_path", "state/copytrader.db"))),
        log_directory=_relative(root, str(copy.get("log_directory", "logs"))),
        snapshot_path=snapshot_path,
        stock_path_prefix=stock_path_prefix,
        etf_path_prefix=etf_path_prefix,
        symbol_aliases=symbol_aliases,
        catalog_hash=str(snapshot.get("catalog_hash")) if snapshot else None,
        catalog_generated_at=str(snapshot.get("generated_at")) if snapshot else None,
        catalog_counts=dict(snapshot.get("counts", {})) if snapshot else {},
        mappings=mappings,
    )
