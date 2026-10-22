"""Shared, stale-while-revalidate market snapshots for the overview workspaces.

Only this owner fetches overview history. Readers retain an immutable snapshot
until a complete refresh finishes; selecting a workspace never starts IO.
"""
from __future__ import annotations

import logging
import math
import sqlite3
import threading
import time

from PySide6 import QtCore

from .models import Candle
from .networking.binance import ApiTask
from .research_data import completed_history

log = logging.getLogger(__name__)
REFRESH_MS = 180_000
HOUR = 3_600_000
QUARTER = 900_000
DAY = 86_400_000
BENCHMARK = "BTCUSDT"
BATCH_SIZE = 24  # The REST collector limits concurrent requests to six.
# field, REST component, cache interval, bar width, retained bars
COMPONENTS = (
    ("series", "futures_1h", "1h", HOUR, 200),
    ("spot_series", "spot_1h", "spot:1h", HOUR, 200),
    ("futures_15m", "futures_15m", "15m", QUARTER, 64),
    ("spot_15m", "spot_15m", "spot:15m", QUARTER, 64),
    ("daily", "daily", "1d", DAY, 25),
)


def _save_snapshot(db, name, snapshot):
    payload = dict(snapshot)
    for field, _component, _interval, _step, _count in COMPONENTS:
        payload[field] = {
            symbol: [[bar.time, bar.open, bar.high, bar.low, bar.close,
                      bar.volume, bar.quote_volume] for _, bar in sorted(rows.items())]
            for symbol, rows in snapshot[field].items()
        }
    try:
        db.save_workspace_snapshot(name, payload)
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        log.warning("Could not save market overview: %s", exc)


def _restore_snapshot(db, name):
    payload = db.load_workspace_snapshot(name)
    if not payload or payload.get("version") != 1:
        return _restore_legacy_snapshot(db, name)
    if not isinstance(payload.get("symbols"), list) or not all(
            isinstance(symbol, str) for symbol in payload["symbols"]):
        raise ValueError("Invalid overview universe")
    for field in ("categories", "tickers", "attempted", "errors"):
        if not isinstance(payload.get(field), dict):
            raise ValueError("Invalid overview metadata")
    if not all(isinstance(value, dict) for field in ("tickers", "attempted", "errors")
               for value in payload[field].values()):
        raise ValueError("Invalid overview market metadata")
    if (int(payload["end"]) % HOUR or int(payload["end_15m"]) % QUARTER
            or int(payload["end_15m"]) // HOUR * HOUR != int(payload["end"])):
        raise ValueError("Inconsistent overview clocks")
    for field, _component, _interval, step, count in COMPONENTS:
        end = int(payload["end_15m"] if step == QUARTER else payload["end"])
        end = end // step * step
        if end <= 0:
            raise ValueError("Invalid overview clock")
        restored = {}
        for symbol, values in payload[field].items():
            if not isinstance(symbol, str) or not isinstance(values, list) or len(values) > count:
                raise ValueError("Invalid overview history")
            rows = []
            for row in values:
                if len(row) != 7:
                    raise ValueError("Invalid overview candle")
                converted = []
                for value in row:
                    number = float(value)
                    if not math.isfinite(number):
                        raise ValueError("Invalid overview candle")
                    converted.append(number)
                candle = Candle(*converted)
                if (min(candle.open, candle.high, candle.low, candle.close) <= 0
                        or min(candle.volume, candle.quote_volume) < 0
                        or candle.low > min(candle.open, candle.close)
                        or candle.high < max(candle.open, candle.close)):
                    raise ValueError("Invalid overview candle range")
                rows.append(candle)
            restored[symbol] = completed_history(rows, step, end, end - count * step)
        payload[field] = restored
    if not math.isfinite(float(payload.get("saved_at", 0))):
        raise ValueError("Invalid overview timestamp")
    return payload


def _restore_legacy_snapshot(db, name):
    # Upgrades can use the existing Leaders/candle caches immediately, before
    # the first shared snapshot has been saved. Import after widget setup.
    from .leadership import load_leadership_state

    state = load_leadership_state(db, name.replace("overview:v1:", "leaders:v2:"))
    if not state:
        return None
    end = state["end"]
    symbols = state["symbols"]
    categories = {symbol: state["categories"].get(symbol, "—") for symbol in symbols}
    quotes = {}
    for symbol, rows in state["series"].items():
        quotes[symbol] = {
            "q": sum(bar.quote_volume for stamp, bar in rows.items() if stamp > end - 24 * HOUR),
            "c": rows[end].close if end in rows else 0,
        }
    snapshot = dict(version=1, saved_at=0, complete=False, end=end, end_15m=end, symbols=symbols,
                    categories=categories, tickers=quotes, attempted={}, errors={},
                    series=state["series"], spot_series=state["spot_series"])
    for field, _component, interval, step, count in COMPONENTS[2:]:
        snapshot[field] = {}
        target = end // step * step
        for symbol in [BENCHMARK, *symbols]:
            if symbol != BENCHMARK and categories.get(symbol, "—") == "—":
                continue
            if symbol == BENCHMARK and field in {"spot_15m", "daily"}:
                continue
            rows = db.load_candles(symbol, "research:" + interval, target - count * step, target - 1)
            snapshot[field][symbol] = completed_history(rows, step, target, target - count * step)
    return snapshot


def load_snapshot(rest, db, symbols, categories, tickers, previous, cancel):
    """Collect a whole generation off the GUI thread, reusing in-memory bars.

    The existing REST collector supplies bounded concurrency, rate limits,
    cancellation and request priorities. No per-pair GUI completions are sent.
    """
    previous = previous or {}
    now = rest.cached_timestamp_ms() - 2000
    end, quarter = int(now // HOUR) * HOUR, int(now // QUARTER) * QUARTER
    wanted = [BENCHMARK, *symbols]
    snapshot = dict(version=1, end=end, end_15m=quarter, symbols=list(symbols),
                    categories=dict(categories), tickers=tickers, errors={}, attempted={})
    for field, _component, _interval, _step, _count in COMPONENTS:
        snapshot[field] = {symbol: rows for symbol, rows in previous.get(field, {}).items()
                           if symbol in categories or symbol == BENCHMARK}
    for offset in range(0, len(wanted), BATCH_SIZE):
        if cancel.is_set():
            return None
        requests = {}
        for symbol in wanted[offset:offset + BATCH_SIZE]:
            sector = categories.get(symbol, "—")
            classified = sector != "—" or symbol == BENCHMARK
            spec = {}
            attempts = snapshot["attempted"].setdefault(symbol, {})
            for field, component, interval, step, count in COMPONENTS:
                if (step != HOUR and not classified
                        or symbol == BENCHMARK and (field.startswith("spot") or field == "daily")):
                    continue
                target = quarter if step == QUARTER else end // step * step
                old_attempt = previous.get("attempted", {}).get(symbol, {}).get(component)
                rows = snapshot[field].get(symbol)
                if rows is None:
                    try:
                        cached = db.load_candles(symbol, "research:" + interval,
                                                 target - count * step, target - 1)
                    except (OSError, sqlite3.Error) as exc:
                        log.warning("Could not restore overview candles: %s", exc)
                        cached = []
                    rows = completed_history(cached, step, target, target - count * step)
                rows = {stamp: bar for stamp, bar in rows.items()
                        if target - count * step < stamp <= target}
                snapshot[field][symbol] = rows
                missing = next((stamp for stamp in range(target - (count - 1) * step,
                                                        target + 1, step) if stamp not in rows), None)
                if missing is None or old_attempt == target:
                    attempts[component] = target
                    continue
                spec[component] = True
                spec[component + "_limit"] = min(count, max(2, (target - missing) // step + 1))
            if spec:
                requests[symbol] = spec
        if not requests:
            continue
        fetched = rest.sector_snapshot_batch(requests, quarter, end)
        for symbol, spec in requests.items():
            if cancel.is_set():
                return None
            entry = fetched.get(symbol, {})
            for field, component, interval, step, count in COMPONENTS:
                if not spec.get(component):
                    continue
                target = quarter if step == QUARTER else end // step * step
                bars = entry.get(component) or []
                rows = dict(snapshot[field].get(symbol, {}))
                incoming = completed_history(bars, step, target, target - count * step)
                rows.update(incoming)
                snapshot[field][symbol] = rows
                if incoming:
                    try:
                        db.cache_candles(symbol, "research:" + interval, list(incoming.values()))
                    except (OSError, sqlite3.Error) as exc:
                        log.warning("Could not cache overview candles: %s", exc)
                error = entry.get("errors", {}).get(component)
                unavailable = component in entry.get("_spot_unavailable", ())
                # Short contiguous histories are legitimate for new listings;
                # retrying them cannot manufacture older bars.
                exhausted = bool(rows and target in rows and all(
                    stamp in rows for stamp in range(min(rows), target + 1, step)))
                if not error and (exhausted or unavailable):
                    snapshot["attempted"][symbol][component] = target
                else:
                    snapshot["errors"].setdefault(symbol, {})[component] = (
                        str(error) if error else "Completed history unavailable")
    snapshot["saved_at"] = time.time()
    snapshot["complete"] = not snapshot["errors"]
    return snapshot


class MarketOverviewCache(QtCore.QObject):
    """Always-warm snapshots; activation and ticker events never force refreshes."""

    changed = QtCore.Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rest = self.db = None
        self.snapshot = self._working = None
        self.symbols = ()
        self.categories = {}
        self.tickers = {}
        self.task = None
        self.cancel = threading.Event()
        self.closing = self.restoring = self.paused = False
        self.next_refresh = 0.0
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)

    def bind(self, rest, db):
        if self.rest is rest and self.db is db:
            return
        self._cancel()
        self.rest, self.db = rest, db
        self.snapshot = self._working = None
        self.next_refresh = 0.0
        self.name = "overview:v1:" + ("testnet" if getattr(rest, "testnet", False) else "live")
        self.restoring = True
        name = self.name
        self._start(lambda: _restore_snapshot(db, name), self._restored)

    def configure(self, symbols, categories, tickers):
        self.symbols = tuple(sorted(symbol for symbol in symbols if symbol != BENCHMARK))
        self.categories = {symbol: categories.get(symbol, "—") for symbol in self.symbols}
        self.tickers = tickers
        self.refresh()

    def _start(self, function, finished):
        task = ApiTask(function, defer_rate_waits=True, source="Market overview snapshot")
        self.task = task
        task.signals.finished.connect(lambda value: self._finished(task, finished, value))
        task.signals.failed.connect(lambda error: self._failed(task, error))
        QtCore.QThreadPool.globalInstance().start(task, -1)

    def _finished(self, task, callback, value):
        if self.closing or self.task is not task:
            return
        self.task = None
        callback(value)

    def _restored(self, snapshot):
        self.restoring = False
        if snapshot:
            self.snapshot = self._working = snapshot
            age = max(0.0, time.time() - float(snapshot.get("saved_at", 0)))
            self.next_refresh = time.monotonic() + max(0.0, REFRESH_MS / 1000 - age)
            self.changed.emit(snapshot)
        self.refresh()

    def refresh(self, *, force=False):
        if self.closing or self.restoring or self.task is not None or self.paused:
            return
        if self.rest is None or self.db is None or not self.symbols or not self.tickers:
            return
        remaining = self.next_refresh - time.monotonic()
        if not force and remaining > 0:
            self.timer.start(max(1, math.ceil(remaining * 1000)))
            return
        rest, db = self.rest, self.db
        symbols, categories = self.symbols, dict(self.categories)
        tickers = {symbol: {"q": row.get("q"), "c": row.get("c")}
                   for symbol, row in self.tickers.items()}
        previous, self.cancel = self._working, threading.Event()
        cancel, name = self.cancel, self.name
        published = self.snapshot

        def collect():
            snapshot = load_snapshot(rest, db, symbols, categories, tickers, previous, cancel)
            if snapshot is None:
                return None
            # Keep a complete visible generation across temporary exchange
            # failures, while retaining successful new bars for the next retry.
            publish = snapshot["complete"] or not published or not published.get("complete", False)
            if publish and not cancel.is_set():
                _save_snapshot(db, name, snapshot)
            return snapshot, publish

        self.timer.stop()
        self._start(collect, self._collected)

    def _collected(self, result):
        if result is not None:
            self._working, publish = result
            if publish:
                self.snapshot = self._working
                self.changed.emit(self.snapshot)
            else:
                log.warning("Keeping complete market overview during history retry")
        self._schedule()

    def _failed(self, task, error):
        if self.closing or self.task is not task:
            return
        self.task = None
        log.warning("Market overview refresh failed: %s", error)
        if self.restoring:
            self._restored(None)
        else:
            self._schedule()

    def _schedule(self):
        self.next_refresh = time.monotonic() + REFRESH_MS / 1000
        self.timer.start(REFRESH_MS)

    def set_paused(self, paused):
        self.paused = bool(paused)
        if not paused:
            self.refresh()

    def _cancel(self):
        self.timer.stop()
        self.cancel.set()
        if self.task is not None:
            self.task.cancel()
            self.task = None

    def shutdown(self):
        self.closing = True
        self._cancel()
