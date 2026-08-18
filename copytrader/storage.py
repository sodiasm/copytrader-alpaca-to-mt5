from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .models import TradeEvent


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _initialize(self) -> None:
        with self.transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    execution_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    price TEXT NOT NULL,
                    position_quantity TEXT,
                    event_timestamp TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS actions (
                    execution_id TEXT PRIMARY KEY REFERENCES events(execution_id),
                    correlation TEXT NOT NULL UNIQUE,
                    target_symbol TEXT NOT NULL,
                    requested_volume TEXT NOT NULL,
                    executed_volume TEXT NOT NULL DEFAULT '0',
                    order_ticket TEXT,
                    deal_ticket TEXT,
                    retcode INTEGER,
                    status TEXT NOT NULL,
                    detail TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS allocations (
                    source_symbol TEXT PRIMARY KEY,
                    target_symbol TEXT NOT NULL,
                    source_quantity TEXT NOT NULL,
                    target_volume TEXT NOT NULL,
                    residual_lots TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paused_symbols (
                    source_symbol TEXT PRIMARY KEY,
                    reason TEXT NOT NULL,
                    paused_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reconciliation_plans (
                    plan_id TEXT PRIMARY KEY,
                    snapshot_hash TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def correlation(execution_id: str) -> str:
        digest = hashlib.sha256(execution_id.encode("utf-8")).hexdigest()[:16]
        return f"cp-{digest}"

    def record_event(self, event: TradeEvent) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO events (
                    execution_id, event_type, order_id, symbol, side, quantity,
                    price, position_quantity, event_timestamp, received_at,
                    status, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'received', ?)
                """,
                (
                    event.execution_id,
                    event.event_type,
                    event.order_id,
                    event.symbol,
                    event.side,
                    str(event.quantity),
                    str(event.price),
                    str(event.position_quantity) if event.position_quantity is not None else None,
                    event.timestamp,
                    utc_now(),
                    json.dumps(event.raw, sort_keys=True, separators=(",", ":"), default=str),
                ),
            )
            return cursor.rowcount == 1

    def update_event(self, execution_id: str, status: str, error: str | None = None) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE events SET status = ?, error = ? WHERE execution_id = ?",
                (status, error, execution_id),
            )

    def event_status(self, execution_id: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT status FROM events WHERE execution_id = ?", (execution_id,)
            ).fetchone()
            return str(row[0]) if row else None

    def pending_events(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT raw_json FROM events
                WHERE status IN ('received', 'processing')
                ORDER BY received_at
                """
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def order_events(self, order_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            return [dict(row) for row in connection.execute(
                """
                SELECT execution_id, side, quantity, status, error
                FROM events WHERE order_id = ? ORDER BY received_at
                """,
                (order_id,),
            )]

    def create_action(
        self,
        execution_id: str,
        target_symbol: str,
        requested_volume: Decimal,
    ) -> str:
        correlation = self.correlation(execution_id)
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO actions (
                    execution_id, correlation, target_symbol, requested_volume,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'planned', ?, ?)
                """,
                (execution_id, correlation, target_symbol, str(requested_volume), now, now),
            )
        return correlation

    def complete_action(
        self,
        execution_id: str,
        *,
        status: str,
        executed_volume: Decimal,
        order_ticket: str = "",
        deal_ticket: str = "",
        retcode: int | None = None,
        detail: str = "",
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE actions SET status = ?, executed_volume = ?, order_ticket = ?,
                    deal_ticket = ?, retcode = ?, detail = ?, updated_at = ?
                WHERE execution_id = ?
                """,
                (
                    status,
                    str(executed_volume),
                    order_ticket,
                    deal_ticket,
                    retcode,
                    detail,
                    utc_now(),
                    execution_id,
                ),
            )

    def action(self, execution_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM actions WHERE execution_id = ?", (execution_id,)
            ).fetchone()
            return dict(row) if row else None

    def allocation(self, source_symbol: str, target_symbol: str) -> tuple[Decimal, Decimal, Decimal]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM allocations WHERE source_symbol = ?", (source_symbol,)
            ).fetchone()
        if not row:
            return Decimal("0"), Decimal("0"), Decimal("0")
        if row["target_symbol"] != target_symbol:
            raise RuntimeError(f"stored target mapping changed for {source_symbol}")
        return (
            Decimal(row["source_quantity"]),
            Decimal(row["target_volume"]),
            Decimal(row["residual_lots"]),
        )

    def set_allocation(
        self,
        source_symbol: str,
        target_symbol: str,
        source_quantity: Decimal,
        target_volume: Decimal,
        residual_lots: Decimal,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO allocations (
                    source_symbol, target_symbol, source_quantity, target_volume,
                    residual_lots, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_symbol) DO UPDATE SET
                    target_symbol = excluded.target_symbol,
                    source_quantity = excluded.source_quantity,
                    target_volume = excluded.target_volume,
                    residual_lots = excluded.residual_lots,
                    updated_at = excluded.updated_at
                """,
                (
                    source_symbol,
                    target_symbol,
                    str(source_quantity),
                    str(target_volume),
                    str(residual_lots),
                    utc_now(),
                ),
            )

    def commit_copy_state(
        self,
        *,
        execution_id: str,
        source_symbol: str,
        target_symbol: str,
        source_quantity: Decimal,
        target_volume: Decimal,
        residual_lots: Decimal,
        event_status: str,
        action_status: str,
        requested_volume: Decimal,
        executed_volume: Decimal,
        order_ticket: str = "",
        deal_ticket: str = "",
        retcode: int | None = None,
        detail: str = "",
    ) -> None:
        now = utc_now()
        correlation = self.correlation(execution_id)
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO allocations (
                    source_symbol, target_symbol, source_quantity, target_volume,
                    residual_lots, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_symbol) DO UPDATE SET
                    target_symbol = excluded.target_symbol,
                    source_quantity = excluded.source_quantity,
                    target_volume = excluded.target_volume,
                    residual_lots = excluded.residual_lots,
                    updated_at = excluded.updated_at
                """,
                (
                    source_symbol,
                    target_symbol,
                    str(source_quantity),
                    str(target_volume),
                    str(residual_lots),
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO actions (
                    execution_id, correlation, target_symbol, requested_volume,
                    executed_volume, order_ticket, deal_ticket, retcode, status,
                    detail, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(execution_id) DO UPDATE SET
                    requested_volume = excluded.requested_volume,
                    executed_volume = excluded.executed_volume,
                    order_ticket = excluded.order_ticket,
                    deal_ticket = excluded.deal_ticket,
                    retcode = excluded.retcode,
                    status = excluded.status,
                    detail = excluded.detail,
                    updated_at = excluded.updated_at
                """,
                (
                    execution_id,
                    correlation,
                    target_symbol,
                    str(requested_volume),
                    str(executed_volume),
                    order_ticket,
                    deal_ticket,
                    retcode,
                    action_status,
                    detail,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE events SET status = ?, error = NULL WHERE execution_id = ?",
                (event_status, execution_id),
            )

    def pause(self, source_symbol: str, reason: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO paused_symbols (source_symbol, reason, paused_at)
                VALUES (?, ?, ?)
                ON CONFLICT(source_symbol) DO UPDATE SET
                    reason = excluded.reason, paused_at = excluded.paused_at
                """,
                (source_symbol, reason, utc_now()),
            )

    def is_paused(self, source_symbol: str) -> bool:
        with closing(self._connect()) as connection:
            return connection.execute(
                "SELECT 1 FROM paused_symbols WHERE source_symbol = ?", (source_symbol,)
            ).fetchone() is not None

    def unpause(self, source_symbol: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM paused_symbols WHERE source_symbol = ?", (source_symbol,)
            )

    def set_state(self, key: str, value: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, utc_now()),
            )

    def delete_state(self, key: str) -> None:
        with self.transaction() as connection:
            connection.execute("DELETE FROM runtime_state WHERE key = ?", (key,))

    def get_state(self, key: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT value FROM runtime_state WHERE key = ?", (key,)
            ).fetchone()
            return str(row[0]) if row else None

    def pause_all(self, reason: str) -> None:
        self.set_state("global_pause_reason", reason)

    def global_pause_reason(self) -> str | None:
        return self.get_state("global_pause_reason")

    def clear_global_pause(self) -> None:
        self.delete_state("global_pause_reason")

    def paused_symbols(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM paused_symbols ORDER BY source_symbol"
                )
            ]

    def has_history(self) -> bool:
        with closing(self._connect()) as connection:
            return connection.execute(
                "SELECT 1 FROM events LIMIT 1"
            ).fetchone() is not None

    def status(self) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            allocations = [dict(row) for row in connection.execute(
                "SELECT * FROM allocations ORDER BY source_symbol"
            )]
            latest = connection.execute(
                """
                SELECT execution_id, symbol, side, quantity, price, status,
                       event_timestamp, error
                FROM events ORDER BY received_at DESC LIMIT 1
                """
            ).fetchone()
            counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM events GROUP BY status"
                )
            }
        return {
            "database": str(self.path),
            "last_event": dict(latest) if latest else None,
            "event_counts": counts,
            "paused_symbols": self.paused_symbols(),
            "allocations": allocations,
            "stream_state": self.get_state("stream_state") or "never_started",
            "global_pause_reason": self.global_pause_reason(),
            "active_universe_hash": self.get_state("active_universe_hash"),
            "active_universe_counts": self.get_state("active_universe_counts"),
        }

    def unresolved_actions(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM actions WHERE status IN ('planned', 'sending', 'ambiguous')"
            )]

    def resolve_action_without_retry(self, execution_id: str, detail: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE actions SET status = 'operator_resolved', detail = ?, updated_at = ?
                WHERE execution_id = ? AND status IN ('planned', 'sending', 'ambiguous')
                """,
                (detail, utc_now(), execution_id),
            )
            connection.execute(
                """
                UPDATE events SET status = 'operator_resolved', error = ?
                WHERE execution_id = ?
                """,
                (detail, execution_id),
            )

    def save_reconciliation_plan(
        self,
        snapshot: dict[str, Any],
        *,
        expires_at: str,
    ) -> str:
        canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        plan_id = digest[:20]
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO reconciliation_plans (
                    plan_id, snapshot_hash, snapshot_json, status, created_at, expires_at
                ) VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (plan_id, digest, canonical, utc_now(), expires_at),
            )
        return plan_id

    def reconciliation_plan(self, plan_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM reconciliation_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            return dict(row) if row else None

    def mark_plan(self, plan_id: str, status: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE reconciliation_plans SET status = ? WHERE plan_id = ?",
                (status, plan_id),
            )
