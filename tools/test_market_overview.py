#!/usr/bin/env python3
"""Offline snapshot and Qt navigation regressions; no exchange/network access.

Run: QT_QPA_PLATFORM=offscreen python tools/test_market_overview.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6 import QtCore, QtWidgets
from nightwatch import overview
from nightwatch.chart.analysis import shutdown_analysis
from nightwatch.database import AppDatabase
from nightwatch.leadership import LeadershipTimelineWidget, SectorOverviewWidget, RotationScannerWidget
from nightwatch.models import Candle
from nightwatch.theme import NIGHTWATCH_COLORS, ui_palette


class MemoryDB:
    def __init__(self):
        self.snapshots = {}
        self.candles = {}
        self.reads = 0

    def load_workspace_snapshot(self, name):
        return json.loads(self.snapshots[name]) if name in self.snapshots else None

    def save_workspace_snapshot(self, name, payload):
        self.snapshots[name] = json.dumps(payload, allow_nan=False)

    def load_candles(self, symbol, interval, start, end):
        self.reads += 1
        return [bar for stamp, bar in self.candles.get((symbol, interval), {}).items()
                if start <= stamp <= end]

    def cache_candles(self, symbol, interval, bars):
        self.candles.setdefault((symbol, interval), {}).update(
            {int(round(bar.time * 1000)): bar for bar in bars})


class OfflineRest:
    testnet = False

    def __init__(self):
        self.now = int(time.time() * 1000) // overview.HOUR * overview.HOUR + 3000
        self.calls = []
        self.fail = False
        self.short_symbol = ''
        self.unavailable_symbol = ''
        self.entered = threading.Event()
        self.release = None

    def cached_timestamp_ms(self):
        return self.now

    def sector_snapshot_batch(self, requests, quarter, hour):
        self.calls.append(requests)
        self.entered.set()
        if self.release is not None and not self.release.wait(10):
            raise RuntimeError('Offline fixture timed out')
        output = {}
        for symbol, spec in requests.items():
            entry = output[symbol] = {}
            for _field, component, _interval, step, count in overview.COMPONENTS:
                if not spec.get(component):
                    continue
                if self.fail:
                    entry.setdefault('errors', {})[component] = 'Offline simulated outage'
                    continue
                if symbol == self.unavailable_symbol and component.startswith('spot'):
                    entry.setdefault('_spot_unavailable', []).append(component)
                    continue
                end = quarter if step == overview.QUARTER else hour // step * step
                count = int(spec.get(component + '_limit', count))
                if symbol == self.short_symbol:
                    count = min(count, 3)
                slope = (sum(symbol.encode()) % 13 + 1) * .0001
                bars = []
                for index, stamp in enumerate(range(end - (count-1)*step, end+1, step)):
                    value = 100 * (1 + slope) ** index
                    bars.append(Candle((stamp-step)/1000, value, value*1.02, value*.98,
                                       value*(1+slope), 100, 2_000_000 + index*100))
                entry[component] = bars
        return output


class Watchlist(QtCore.QObject):
    symbols_changed = QtCore.Signal(object)
    icon_changed = QtCore.Signal(str)
    symbols = ()

    def contains(self, symbol):
        return False


def inputs(count=6):
    symbols = tuple(f'COIN{index:03}USDT' for index in range(count))
    sectors = ('AI', 'DeFi', 'Meme')
    categories = {s: sectors[i % len(sectors)] for i, s in enumerate(symbols)}
    tickers = {s: {'q': 90_000_000, 'c': 110} for s in ('BTCUSDT', *symbols)}
    return symbols, categories, tickers


def load(rest, db, previous=None, count=6):
    symbols, categories, tickers = inputs(count)
    return overview.load_snapshot(rest, db, symbols, categories, tickers, previous, threading.Event())


def wait_until(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while not predicate():
        QtWidgets.QApplication.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError('Timed out waiting for the offline snapshot')
        time.sleep(.003)
    QtWidgets.QApplication.processEvents()


class OverviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_whole_snapshot_then_memory_only_refresh_and_small_tails(self):
        rest, db = OfflineRest(), MemoryDB()
        first = load(rest, db, count=51)
        self.assertFalse(first['errors'])
        self.assertEqual(len(first['series']), 52)
        self.assertTrue(all(len(batch) <= overview.BATCH_SIZE for batch in rest.calls))
        self.assertEqual(len(rest.calls), 3)
        reads, calls = db.reads, len(rest.calls)
        second = load(rest, db, first, count=51)
        self.assertEqual(db.reads, reads)
        self.assertEqual(len(rest.calls), calls)
        self.assertEqual(first['series'], second['series'])
        rest.now += overview.HOUR
        third = load(rest, db, second, count=51)
        self.assertFalse(third['errors'])
        self.assertNotEqual(first['end'], third['end'])
        self.assertEqual(max(first['series']['BTCUSDT']), first['end'])
        for batch in rest.calls[calls:]:
            for spec in batch.values():
                self.assertLessEqual(spec.get('futures_1h_limit', 0), 2)
                self.assertLessEqual(spec.get('futures_15m_limit', 0), 4)

    def test_short_listings_and_unavailable_spot_do_not_retry_same_bar(self):
        rest, db = OfflineRest(), MemoryDB()
        rest.short_symbol = 'COIN000USDT'
        rest.unavailable_symbol = 'COIN001USDT'
        first = load(rest, db)
        self.assertFalse(first['errors'])
        self.assertEqual(len(first['series'][rest.short_symbol]), 3)
        calls = len(rest.calls)
        second = load(rest, db, first)
        self.assertFalse(second['errors'])
        self.assertEqual(len(rest.calls), calls)

    def test_sqlite_round_trip_and_corrupt_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            db = AppDatabase(str(Path(root) / 'overview.sqlite'))
            snapshot = load(OfflineRest(), db)
            overview._save_snapshot(db, 'overview:v1:live', snapshot)
            restored = overview._restore_snapshot(db, 'overview:v1:live')
            self.assertEqual(restored, snapshot)
            payload = db.load_workspace_snapshot('overview:v1:live')
            payload['series']['BTCUSDT'][0][2] = -1
            db.save_workspace_snapshot('overview:v1:live', payload)
            with self.assertRaises(ValueError):
                overview._restore_snapshot(db, 'overview:v1:live')

    def test_publish_once_and_keep_last_complete_snapshot_on_failure(self):
        rest, db = OfflineRest(), MemoryDB()
        cache = overview.MarketOverviewCache()
        published = []
        cache.changed.connect(published.append)
        cache.configure(*inputs(51))
        cache.bind(rest, db)
        try:
            wait_until(lambda: cache.snapshot is not None)
            self.assertEqual(len(published), 1)
            self.assertEqual(len(cache.snapshot['symbols']), 51)
            first = cache.snapshot
            rest.now += overview.HOUR
            rest.fail = True
            cache.refresh(force=True)
            wait_until(lambda: cache.task is None)
            self.assertIs(cache.snapshot, first)
            self.assertEqual(len(published), 1)
            rest.fail = False
            cache.refresh(force=True)
            wait_until(lambda: cache.task is None)
            self.assertEqual(len(published), 2)
            self.assertFalse(cache.snapshot['errors'])
            self.assertEqual(cache.timer.interval(), overview.REFRESH_MS)
        finally:
            cache.shutdown()

    def test_shutdown_discards_inflight_generation(self):
        rest, db = OfflineRest(), MemoryDB()
        rest.release = threading.Event()
        cache = overview.MarketOverviewCache()
        published = []
        cache.changed.connect(published.append)
        cache.configure(*inputs())
        cache.bind(rest, db)
        wait_until(rest.entered.is_set)
        cache.shutdown()
        rest.release.set()
        QtCore.QThreadPool.globalInstance().waitForDone(5000)
        self.app.processEvents()
        self.assertFalse(published)
        self.assertFalse(cache.timer.isActive())

    def test_existing_leaders_cache_is_available_during_upgrade(self):
        from nightwatch.leadership import encode_histories
        rest, db = OfflineRest(), MemoryDB()
        snapshot = load(rest, db)
        db.save_workspace_snapshot('leaders:v2:live', {
            'version': 2, 'end': snapshot['end'], 'symbols': snapshot['symbols'],
            'categories': snapshot['categories'],
            'series': encode_histories(snapshot['series'], snapshot['end']),
            'spot': encode_histories(snapshot['spot_series'], snapshot['end']),
        })
        restored = overview._restore_snapshot(db, 'overview:v1:live')
        self.assertEqual(restored['series'], snapshot['series'])
        self.assertTrue(restored['futures_15m']['BTCUSDT'])
        self.assertFalse(restored['complete'])
        self.assertGreater(restored['tickers']['COIN000USDT']['q'], 20_000_000)

    def test_cold_start_loads_hidden_views_and_survives_tab_changes(self):
        rest, db = OfflineRest(), MemoryDB()
        rest.release = threading.Event()
        symbols, _categories, quotes = inputs()
        palette = ui_palette(NIGHTWATCH_COLORS)
        leaders, sectors = LeadershipTimelineWidget(palette), SectorOverviewWidget(palette)
        rotation, watchlist = RotationScannerWidget(palette), Watchlist()
        views = (leaders, sectors, rotation)
        rotation.bind_leadership(leaders)
        leaders.set_universe({'BTCUSDT', *symbols}, [
            {'symbol': symbol, 'underlyingSubType': ['ai']} for symbol in symbols])
        leaders.set_tickers(quotes)
        sectors.bind_sources(rest, db, leaders, lambda: False)
        leaders.bind_sources(rest, db, watchlist, lambda: False)
        try:
            wait_until(rest.entered.is_set)
            task = leaders._overview.task
            for view in views:
                view.set_active(True)
                view.set_active(False)
            self.assertIs(leaders._overview.task, task)
            self.assertFalse(task.cancelled.is_set())
            rest.release.set()
            wait_until(lambda: all(view._prepared_analysis and not view._render_dirty for view in views))
            self.assertEqual(leaders.table.rowCount(), len(symbols))
            self.assertEqual(len(rotation._prepared_analysis['points']), len(symbols))
            self.assertEqual(sectors.table.rowCount(), 1)
            self.assertTrue(all(not view.active for view in views))
        finally:
            rest.release.set()
            for view in views:
                view.shutdown()
            QtCore.QThreadPool.globalInstance().waitForDone(5000)

    def test_hidden_views_restore_and_switch_without_loading_or_analysis(self):
        rest, db = OfflineRest(), MemoryDB()
        snapshot = load(rest, db, count=96)
        overview._save_snapshot(db, 'overview:v1:live', snapshot)
        rest.calls.clear()
        palette = ui_palette(NIGHTWATCH_COLORS)
        leaders = LeadershipTimelineWidget(palette)
        leaders.limit.setCurrentIndex(leaders.limit.findData(0))
        sectors = SectorOverviewWidget(palette)
        rotation = RotationScannerWidget(palette)
        watchlist = Watchlist()
        stack = QtWidgets.QStackedWidget()
        stack.resize(1440, 900)
        stack.addWidget(QtWidgets.QWidget())
        for view in (leaders, sectors, rotation):
            stack.addWidget(view)
        rotation.bind_leadership(leaders)
        sectors.bind_sources(rest, db, leaders, lambda: False)
        leaders.bind_sources(rest, db, watchlist, lambda: False)
        try:
            wait_until(lambda: all(v._prepared_analysis and not v._render_dirty
                                   for v in (leaders, sectors, rotation)))
            self.assertEqual(leaders.table.rowCount(), 96)
            self.assertGreater(sectors.table.rowCount(), 0)
            self.assertEqual(len(rotation._prepared_analysis['points']), 96)
            self.assertFalse(rest.calls)
            serials = [v._analysis_serial for v in (leaders, sectors, rotation)]
            stack.show()
            self.app.processEvents()
            timings = []
            switch_only = []
            by_view = {index: [] for index in range(4)}
            for _ in range(20):
                for index in (1, 2, 3, 0):
                    started = time.perf_counter()
                    stack.setCurrentIndex(index)
                    leaders.set_active(index == 1, visible=index == 1)
                    sectors.set_active(index == 2)
                    rotation.set_active(index == 3)
                    switch_only.append((time.perf_counter() - started) * 1000)
                    self.app.processEvents()
                    timings.append((time.perf_counter() - started) * 1000)
                    by_view[index].append(timings[-1])
                    self.assertTrue(all(v._prepared_analysis for v in (leaders, sectors, rotation)))
            self.assertEqual(serials, [v._analysis_serial for v in (leaders, sectors, rotation)])
            self.assertFalse(rest.calls)
            print(f'\nWarm switches incl. offscreen paint: median={sorted(timings)[len(timings)//2]:.2f} ms; '
                  f'p95={sorted(timings)[int(len(timings)*.95)]:.2f} ms; 0 fetches, 0 analyses')
            print('Activation median %.2f ms; paint by page %s' % (sorted(switch_only)[len(switch_only)//2], {key: round(sorted(values)[len(values)//2], 2) for key, values in by_view.items()}))
            # Cached cells must preserve their pixels and invalidate on edits.
            viewport = leaders.table.viewport()
            delegate = leaders.table.itemDelegate()
            first_image = viewport.grab().toImage()
            with patch.object(delegate, '_paint_cell', wraps=delegate._paint_cell) as paint:
                self.assertEqual(viewport.grab().toImage(), first_image)
                self.assertEqual(paint.call_count, 0)
                leaders.table.item(0, 4).setText('CHANGED')
                self.assertNotEqual(viewport.grab().toImage(), first_image)
                self.assertGreater(paint.call_count, 0)
            # Leaders' filters must not shrink Rotation's cached universe.
            rotation_count = len(rotation.symbols)
            leaders.limit.setCurrentIndex(leaders.limit.findData(40))
            wait_until(lambda: not leaders._render_dirty)
            self.assertEqual(leaders.table.rowCount(), 40)
            self.assertEqual(len(rotation.symbols), rotation_count)
            self.assertFalse(rest.calls)
            # Snapshot adoption is postponed during chart interaction, without
            # discarding the already-rendered old generation.
            sectors.set_interaction_priority(True)
            old = sectors._market_snapshot
            refreshed = dict(snapshot, saved_at=snapshot['saved_at']+180)
            sectors._overview_changed(refreshed)
            self.assertIs(sectors._market_snapshot, old)
            self.assertGreater(sectors.table.rowCount(), 0)
            sectors.set_interaction_priority(False)
            wait_until(lambda: not sectors._render_dirty)
            self.assertIs(sectors._market_snapshot, refreshed)
        finally:
            for view in (leaders, sectors, rotation):
                view.shutdown()
            stack.close()
            QtCore.QThreadPool.globalInstance().waitForDone(5000)


if __name__ == '__main__':
    try:
        unittest.main(verbosity=2)
    finally:
        shutdown_analysis()
