"""SQLite persistence for candles and recorded market events."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager

from .constants import INTERVAL_SECONDS
from .models import Candle


class AppDatabase:
    """Durable candle cache and recorded market-event store."""

    def __init__(self, path: str):
        self.path = path
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Own one transaction and always release its connection, including failures."""
        connection = sqlite3.connect(self.path, timeout=30.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA busy_timeout=30000")
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS candle_cache (
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    open_time INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    quote_volume REAL NOT NULL,
                    PRIMARY KEY (symbol, interval, open_time)
                );

                CREATE TABLE IF NOT EXISTS candle_coverage (
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    first_time INTEGER NOT NULL,
                    last_time INTEGER NOT NULL,
                    complete_from_listing INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (symbol, interval)
                );

                CREATE TABLE IF NOT EXISTS market_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_time INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS market_events_range
                    ON market_events(symbol, event_type, event_time);

                CREATE TABLE IF NOT EXISTS event_coverage (
                    symbol TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    first_time INTEGER NOT NULL,
                    last_time INTEGER NOT NULL,
                    complete_from_listing INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (symbol, event_type)
                );

                CREATE TABLE IF NOT EXISTS archive_imports (
                    archive_key TEXT PRIMARY KEY,
                    dataset TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    imported_rows INTEGER NOT NULL,
                    imported_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspace_snapshots (
                    name TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS coin_icon_cache (
                    base_symbol TEXT PRIMARY KEY,
                    provider TEXT NOT NULL DEFAULT '',
                    provider_id TEXT NOT NULL DEFAULT '',
                    remote_symbol TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '',
                    filename TEXT NOT NULL DEFAULT '',
                    image_url TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'missing',
                    refreshed_at INTEGER NOT NULL DEFAULT 0,
                    attempted_at INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS coin_icon_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )


            unique_index = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'index' AND name = 'market_events_unique'
                """
            ).fetchone()
            if unique_index is None:
                connection.execute(
                    """
                    DELETE FROM market_events
                    WHERE id NOT IN (
                        SELECT MIN(id)
                        FROM market_events
                        GROUP BY symbol, event_type, event_time
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE UNIQUE INDEX market_events_unique
                    ON market_events(symbol, event_type, event_time)
                    """
                )
            connection.execute("DROP INDEX IF EXISTS market_events_range")
            connection.execute("DROP INDEX IF EXISTS candle_cache_range")


    def cache_candles(self, symbol: str, interval: str, candles: list[Candle]) -> None:
        if not candles:
            return
        rows = [
            (
                symbol,
                interval,
                int(candle.time * 1000),
                candle.open,
                candle.high,
                candle.low,
                candle.close,
                candle.volume,
                candle.quote_volume,
            )
            for candle in candles
        ]
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO candle_cache
                (symbol, interval, open_time, open, high, low, close, volume, quote_volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def load_candles(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> list[Candle]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT open_time, open, high, low, close, volume, quote_volume
                FROM candle_cache
                WHERE symbol = ? AND interval = ? AND open_time BETWEEN ? AND ?
                ORDER BY open_time
                """,
                (symbol, interval, start_ms, end_ms),
            ).fetchall()
        return [
            Candle(
                time=row["open_time"] / 1000.0,
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                quote_volume=row["quote_volume"],
            )
            for row in rows
        ]

    def load_workspace_snapshot(self, name: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM workspace_snapshots WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        return payload if isinstance(payload, dict) else None

    def save_workspace_snapshot(self, name: str, payload: dict) -> None:


        encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO workspace_snapshots (name, payload_json) VALUES (?, ?)",
                (name, encoded),
            )


    def save_coin_icon(
        self,
        base_symbol: str,
        *,
        provider: str,
        provider_id: str,
        remote_symbol: str,
        name: str,
        filename: str,
        image_url: str,
    ) -> None:
        base = str(base_symbol or "").upper().strip()
        if not base:
            return
        now = int(time.time() * 1000)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO coin_icon_cache
                (base_symbol, provider, provider_id, remote_symbol, name, filename,
                 image_url, status, refreshed_at, attempted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?)
                ON CONFLICT(base_symbol) DO UPDATE SET
                    provider = excluded.provider,
                    provider_id = excluded.provider_id,
                    remote_symbol = excluded.remote_symbol,
                    name = excluded.name,
                    filename = excluded.filename,
                    image_url = excluded.image_url,
                    status = 'ready',
                    refreshed_at = excluded.refreshed_at,
                    attempted_at = excluded.attempted_at
                """,
                (
                    base,
                    str(provider or ""),
                    str(provider_id or ""),
                    str(remote_symbol or "").upper(),
                    str(name or ""),
                    str(filename or ""),
                    str(image_url or ""),
                    now,
                    now,
                ),
            )

    def mark_coin_icon_missing(self, base_symbol: str) -> None:
        base = str(base_symbol or "").upper().strip()
        if not base:
            return
        now = int(time.time() * 1000)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO coin_icon_cache
                (base_symbol, status, attempted_at)
                VALUES (?, 'missing', ?)
                ON CONFLICT(base_symbol) DO UPDATE SET
                    status = 'missing',
                    attempted_at = excluded.attempted_at
                """,
                (base, now),
            )

    def coin_icon_retry_due(self, base_symbol: str, interval_ms: int) -> bool:
        base = str(base_symbol or "").upper().strip()
        if not base:
            return False
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status, attempted_at FROM coin_icon_cache WHERE base_symbol = ?",
                (base,),
            ).fetchone()
        if row is None or str(row["status"]) != "missing":
            return True
        return int(time.time() * 1000) - int(row["attempted_at"] or 0) >= max(0, int(interval_ms))

    def claim_coin_icon_full_refresh(self, interval_ms: int) -> bool:
        """Atomically claim the at-most-weekly active icon refresh for this install."""
        now = int(time.time() * 1000)
        interval = max(0, int(interval_ms))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value FROM coin_icon_state WHERE key = 'full_refresh_attempt_at'"
            ).fetchone()
            try:
                previous = int(row["value"]) if row is not None else 0
            except (TypeError, ValueError):
                previous = 0
            if previous and now - previous < interval:
                return False
            connection.execute(
                """
                INSERT INTO coin_icon_state (key, value) VALUES ('full_refresh_attempt_at', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(now),),
            )
        return True

    def candle_coverage(self, symbol: str, interval: str) -> tuple[int, int, int]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT MIN(open_time) AS first_time, MAX(open_time) AS last_time, COUNT(*) AS rows
                FROM candle_cache WHERE symbol = ? AND interval = ?
                """,
                (symbol, interval),
            ).fetchone()
        return (
            int(row["first_time"] or 0),
            int(row["last_time"] or 0),
            int(row["rows"] or 0),
        )


    def aggregate_candle_intervals(
        self,
        symbol: str,
        source_interval: str,
        target_intervals: tuple[str, ...],
    ) -> dict[str, int]:
        """Derive several UTC-aligned timeframes with one source-table scan."""
        source_ms = INTERVAL_SECONDS[source_interval] * 1000
        targets = tuple(dict.fromkeys(target_intervals))
        target_sizes: dict[str, int] = {}
        for target_interval in targets:
            target_ms = INTERVAL_SECONDS[target_interval] * 1000
            if target_ms <= source_ms or target_ms % source_ms:
                raise ValueError(
                    "Target timeframe must be an exact multiple of the source timeframe."
                )
            target_sizes[target_interval] = target_ms
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT open_time, open, high, low, close, volume, quote_volume
                FROM candle_cache
                WHERE symbol = ? AND interval = ?
                ORDER BY open_time
                """,
                (symbol, source_interval),
            ).fetchall()
        buckets: dict[str, list[Candle]] = {target: [] for target in targets}
        states: dict[str, tuple[int, Candle | None, int]] = {
            target: (-1, None, 0) for target in targets
        }
        for row in rows:
            open_time = int(row["open_time"])
            for target_interval, target_ms in target_sizes.items():
                current_bucket, current, count = states[target_interval]
                bucket = open_time - open_time % target_ms
                if bucket != current_bucket:
                    if current is not None and count == target_ms // source_ms:
                        buckets[target_interval].append(current)
                    current_bucket = bucket
                    count = 0
                    current = Candle(
                        time=bucket / 1000.0,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                        quote_volume=float(row["quote_volume"]),
                    )
                elif current is not None:
                    current.high = max(current.high, float(row["high"]))
                    current.low = min(current.low, float(row["low"]))
                    current.close = float(row["close"])
                    current.volume += float(row["volume"])
                    current.quote_volume += float(row["quote_volume"])
                states[target_interval] = (current_bucket, current, count + 1)
        for target_interval, target_ms in target_sizes.items():
            _current_bucket, current, count = states[target_interval]
            if current is not None and count == target_ms // source_ms:
                buckets[target_interval].append(current)

        source_coverage = self.download_coverage(symbol, source_interval)
        counts: dict[str, int] = {}
        for target_interval in targets:
            target_buckets = buckets[target_interval]
            self.cache_candles(symbol, target_interval, target_buckets)
            counts[target_interval] = len(target_buckets)
            if not target_buckets:
                continue
            self.mark_download_coverage(
                symbol,
                target_interval,
                int(target_buckets[0].time * 1000),
                int(target_buckets[-1].time * 1000),
                bool(source_coverage.get("complete_from_listing")),
            )
        return counts

    def download_coverage(self, symbol: str, interval: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT first_time, last_time, complete_from_listing, updated_at
                FROM candle_coverage WHERE symbol = ? AND interval = ?
                """,
                (symbol, interval),
            ).fetchone()
        return dict(row) if row is not None else {}

    def mark_download_coverage(
        self,
        symbol: str,
        interval: str,
        first_time: int,
        last_time: int,
        complete_from_listing: bool,
    ) -> None:
        if first_time <= 0 or last_time <= 0:
            return
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO candle_coverage
                (symbol, interval, first_time, last_time, complete_from_listing, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, interval) DO UPDATE SET
                    first_time = MIN(candle_coverage.first_time, excluded.first_time),
                    last_time = MAX(candle_coverage.last_time, excluded.last_time),
                    complete_from_listing = MAX(
                        candle_coverage.complete_from_listing,
                        excluded.complete_from_listing
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    symbol,
                    interval,
                    first_time,
                    last_time,
                    int(complete_from_listing),
                    int(time.time() * 1000),
                ),
            )

    def insert_market_events(self, events: list[tuple[str, str, int, dict[str, Any]]]) -> None:
        if not events:
            return
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO market_events
                (symbol, event_type, event_time, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        symbol,
                        event_type,
                        int(event_time),
                        json.dumps(payload, separators=(",", ":")),
                    )
                    for symbol, event_type, event_time, payload in events
                ],
            )

    def event_download_coverage(self, symbol: str, event_type: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT first_time, last_time, complete_from_listing, updated_at
                FROM event_coverage WHERE symbol = ? AND event_type = ?
                """,
                (symbol, event_type),
            ).fetchone()
        return dict(row) if row is not None else {}

    def cache_market_event_page(
        self,
        symbol: str,
        event_type: str,
        rows: list[tuple[int, dict[str, Any]]],
        complete_from_listing: bool = False,
    ) -> None:
        """Commit a complete API page and its resume checkpoint atomically."""
        if not rows:
            return
        first_time = min(int(row[0]) for row in rows)
        last_time = max(int(row[0]) for row in rows)
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO market_events
                (symbol, event_type, event_time, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        symbol,
                        event_type,
                        int(event_time),
                        json.dumps(payload, separators=(",", ":")),
                    )
                    for event_time, payload in rows
                ],
            )
            connection.execute(
                """
                INSERT INTO event_coverage
                (symbol, event_type, first_time, last_time, complete_from_listing, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, event_type) DO UPDATE SET
                    first_time = MIN(event_coverage.first_time, excluded.first_time),
                    last_time = MAX(event_coverage.last_time, excluded.last_time),
                    complete_from_listing = MAX(
                        event_coverage.complete_from_listing,
                        excluded.complete_from_listing
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    symbol,
                    event_type,
                    first_time,
                    last_time,
                    int(complete_from_listing),
                    int(time.time() * 1000),
                ),
            )

    def imported_archive_keys(self, prefix: str) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT archive_key FROM archive_imports WHERE archive_key LIKE ?",
                (prefix + "%",),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def mark_archive_imported(
        self,
        archive_key: str,
        dataset: str,
        symbol: str,
        imported_rows: int,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO archive_imports
                (archive_key, dataset, symbol, imported_rows, imported_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    archive_key,
                    dataset,
                    symbol,
                    max(0, int(imported_rows)),
                    int(time.time() * 1000),
                ),
            )


    def load_market_events(
        self,
        symbol: str,
        event_types: tuple[str, ...],
        start_ms: int,
        end_ms: int,
    ) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in event_types)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT m.event_type, m.event_time, m.payload_json
                FROM market_events m
                JOIN (
                    SELECT MAX(id) AS selected_id
                    FROM market_events
                    WHERE symbol = ? AND event_type IN ({placeholders})
                      AND event_time BETWEEN ? AND ?
                    GROUP BY event_type, event_time
                ) selected ON selected.selected_id = m.id
                ORDER BY m.event_time
                """,
                (symbol, *event_types, start_ms, end_ms),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            events.append(
                {
                    "event_type": row["event_type"],
                    "event_time": row["event_time"],
                    "payload": payload,
                }
            )
        return events

    def storage_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            path = self.path + suffix
            if os.path.exists(path):
                total += os.path.getsize(path)
        return total

    def storage_summary(self) -> dict[str, int]:
        with self._connect() as connection:
            return {
                "candles": int(connection.execute("SELECT COUNT(*) FROM candle_cache").fetchone()[0]),
                "events": int(connection.execute("SELECT COUNT(*) FROM market_events").fetchone()[0]),
                "bytes": self.storage_bytes(),
            }

    def clear_storage(self, candles: bool, events: bool) -> None:
        with self._connect() as connection:
            if candles:
                connection.execute("DELETE FROM candle_cache")
                connection.execute("DELETE FROM candle_coverage")
            if events:
                connection.execute("DELETE FROM market_events")
                connection.execute("DELETE FROM event_coverage")
                connection.execute("DELETE FROM archive_imports")
            connection.execute("PRAGMA optimize")
        with self._connect() as connection:
            connection.execute("VACUUM")


def _recorder_spool_path(database: AppDatabase) -> str:
    return database.path + ".recorder-spool.json"


def _read_recorder_spool(database: AppDatabase) -> list[tuple]:
    path = _recorder_spool_path(database)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as source:
        rows = json.load(source)
    if not isinstance(rows, list) or any(
        not isinstance(row, list) or len(row) != 4 for row in rows
    ):
        raise ValueError(
            "The local recorder recovery file is malformed; it has been preserved."
        )
    return [tuple(row) for row in rows]


def commit_market_events(database: AppDatabase, events: list[tuple]) -> None:
    """Commit a worker batch together with any batch recovered after a failed exit."""
    recovered = _read_recorder_spool(database)
    database.insert_market_events([*recovered, *events])
    if recovered:
        os.unlink(_recorder_spool_path(database))


def spool_market_events(database: AppDatabase, events: list[tuple]) -> None:
    """Preserve a failed worker batch with atomic replacement and fsync."""
    path = _recorder_spool_path(database)
    recovered = _read_recorder_spool(database)
    handle, temporary = tempfile.mkstemp(
        prefix="recorder-", suffix=".tmp", dir=os.path.dirname(path)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            json.dump([*recovered, *events], output, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
