from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import ConfigurationError, SymbolMapping


SCHEMA_VERSION = 1


def _canonical_content(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": snapshot["schema_version"],
        "criteria": snapshot["criteria"],
        "counts": snapshot["counts"],
        "mappings": snapshot["mappings"],
        "excluded": snapshot["excluded"],
    }


def catalog_hash(snapshot: dict[str, Any]) -> str:
    encoded = json.dumps(
        _canonical_content(snapshot),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _entry(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    fields = (
        "name",
        "description",
        "path",
        "currency_profit",
        "trade_contract_size",
        "volume_min",
        "volume_step",
        "trade_mode",
    )
    return {field: getattr(value, field, None) for field in fields}


def build_snapshot(
    symbols: Iterable[Any],
    active_alpaca_symbols: set[str],
    *,
    stock_path_prefix: str,
    etf_path_prefix: str,
    aliases: dict[str, str],
    full_trade_mode: int,
) -> dict[str, Any]:
    active = {symbol.strip().upper() for symbol in active_alpaca_symbols if symbol.strip()}
    normalized_aliases = {
        source.strip().upper(): target.strip()
        for source, target in aliases.items()
        if source.strip() and target.strip()
    }
    reverse_aliases: dict[str, list[str]] = {}
    for source, target in normalized_aliases.items():
        reverse_aliases.setdefault(target, []).append(source)

    mappings: list[dict[str, str]] = []
    excluded: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    seen_targets: set[str] = set()

    for raw in symbols:
        row = _entry(raw)
        path = str(row.get("path") or "")
        if path.startswith(stock_path_prefix):
            asset_type = "stock"
        elif path.startswith(etf_path_prefix):
            asset_type = "etf"
        else:
            continue

        target = str(row.get("name") or "").strip()
        description = str(row.get("description") or "")
        base = {
            "target": target,
            "asset_type": asset_type,
            "path": path,
            "description": description,
        }
        if not target:
            excluded.append({**base, "reason": "missing_mt5_symbol"})
            continue
        if int(row.get("trade_mode") or -1) != int(full_trade_mode):
            excluded.append({**base, "reason": "mt5_not_full_trade"})
            continue
        if str(row.get("currency_profit") or "").upper() != "USD":
            excluded.append({**base, "reason": "mt5_profit_currency_not_usd"})
            continue

        exact_source = target.upper()
        if exact_source in active:
            source = exact_source
        else:
            candidates = [
                source
                for source in reverse_aliases.get(target, [])
                if source in active
            ]
            if len(candidates) != 1:
                reason = (
                    "ambiguous_alias"
                    if len(candidates) > 1
                    else "not_active_tradable_on_alpaca"
                )
                excluded.append({**base, "reason": reason})
                continue
            source = candidates[0]

        if source in seen_sources:
            raise ConfigurationError(f"duplicate catalog source mapping: {source}")
        if target in seen_targets:
            raise ConfigurationError(f"duplicate catalog target mapping: {target}")
        seen_sources.add(source)
        seen_targets.add(target)
        mappings.append(
            {"source": source, "target": target, "asset_type": asset_type}
        )

    mappings.sort(key=lambda item: (item["source"], item["target"]))
    excluded.sort(
        key=lambda item: (item["asset_type"], item["target"], item["reason"])
    )
    if not mappings:
        raise ConfigurationError("catalog discovery produced no eligible mappings")

    stock_count = sum(item["asset_type"] == "stock" for item in mappings)
    etf_count = sum(item["asset_type"] == "etf" for item in mappings)
    snapshot: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "criteria": {
            "stock_path_prefix": stock_path_prefix,
            "etf_path_prefix": etf_path_prefix,
            "mt5_trade_mode": "full",
            "currency_profit": "USD",
            "alpaca_asset_class": "us_equity",
            "alpaca_status": "active",
            "alpaca_tradable": True,
            "aliases": dict(sorted(normalized_aliases.items())),
        },
        "counts": {
            "stocks": stock_count,
            "etfs": etf_count,
            "mappings": len(mappings),
            "excluded": len(excluded),
        },
        "mappings": mappings,
        "excluded": excluded,
    }
    snapshot["catalog_hash"] = catalog_hash(snapshot)
    return snapshot


def load_snapshot(
    path: Path,
    *,
    stock_path_prefix: str,
    etf_path_prefix: str,
    aliases: dict[str, str],
) -> tuple[tuple[SymbolMapping, ...], dict[str, Any]]:
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"symbol catalog snapshot not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"invalid symbol catalog snapshot {path}: {exc}") from exc
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != SCHEMA_VERSION:
        raise ConfigurationError(
            f"unsupported symbol catalog schema in {path}; expected {SCHEMA_VERSION}"
        )
    mappings_raw = snapshot.get("mappings")
    if not isinstance(mappings_raw, list) or not mappings_raw:
        raise ConfigurationError(f"symbol catalog has no mappings: {path}")
    criteria = snapshot.get("criteria")
    if not isinstance(criteria, dict):
        raise ConfigurationError(f"symbol catalog criteria are missing: {path}")
    expected_aliases = {
        str(source).strip().upper(): str(target).strip()
        for source, target in aliases.items()
    }
    if criteria.get("stock_path_prefix") != stock_path_prefix:
        raise ConfigurationError("symbol catalog stock path prefix does not match config")
    if criteria.get("etf_path_prefix") != etf_path_prefix:
        raise ConfigurationError("symbol catalog ETF path prefix does not match config")
    if criteria.get("aliases") != dict(sorted(expected_aliases.items())):
        raise ConfigurationError("symbol catalog aliases do not match config")
    stored_hash = str(snapshot.get("catalog_hash") or "")
    if not stored_hash or stored_hash != catalog_hash(snapshot):
        raise ConfigurationError(f"symbol catalog hash validation failed: {path}")

    mappings: list[SymbolMapping] = []
    seen_sources: set[str] = set()
    seen_targets: set[str] = set()
    for entry in mappings_raw:
        if not isinstance(entry, dict):
            raise ConfigurationError("symbol catalog mapping must be an object")
        source = str(entry.get("source") or "").strip().upper()
        target = str(entry.get("target") or "").strip()
        asset_type = str(entry.get("asset_type") or "").strip().lower()
        if not source or not target or asset_type not in {"stock", "etf"}:
            raise ConfigurationError(f"invalid symbol catalog mapping: {entry!r}")
        if source in seen_sources or target in seen_targets:
            raise ConfigurationError(f"duplicate symbol catalog mapping: {source}->{target}")
        seen_sources.add(source)
        seen_targets.add(target)
        mappings.append(SymbolMapping(source, target, asset_type))

    counts = snapshot.get("counts") or {}
    actual_stocks = sum(mapping.asset_type == "stock" for mapping in mappings)
    actual_etfs = sum(mapping.asset_type == "etf" for mapping in mappings)
    if (
        counts.get("stocks") != actual_stocks
        or counts.get("etfs") != actual_etfs
        or counts.get("mappings") != len(mappings)
    ):
        raise ConfigurationError(f"symbol catalog counts do not match mappings: {path}")
    return tuple(mappings), snapshot


def snapshot_diff(
    previous: dict[str, Any] | None, candidate: dict[str, Any]
) -> dict[str, Any]:
    previous_by_source = {
        item["source"]: item for item in (previous or {}).get("mappings", [])
    }
    candidate_by_source = {
        item["source"]: item for item in candidate.get("mappings", [])
    }
    added = sorted(set(candidate_by_source) - set(previous_by_source))
    removed = sorted(set(previous_by_source) - set(candidate_by_source))
    changed = [
        {
            "source": source,
            "before": previous_by_source[source],
            "after": candidate_by_source[source],
        }
        for source in sorted(set(previous_by_source) & set(candidate_by_source))
        if previous_by_source[source] != candidate_by_source[source]
    ]
    return {
        "changed": bool(added or removed or changed),
        "added": added,
        "removed": removed,
        "modified": changed,
    }


def read_snapshot_if_present(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"cannot compare existing symbol catalog {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"existing symbol catalog is not an object: {path}")
    return value


def write_snapshot_atomic(path: Path, snapshot: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
