"""Exercise chart navigation, snapshot handoffs and Qt crosshair restoration."""
from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
import sys
import time
from types import MethodType, SimpleNamespace
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from nightwatch.chart.analysis import shutdown_analysis
from nightwatch.chart.workspace import ChartWorkspace
from nightwatch.models import Candle, CandlePages, shift_candle_time
from nightwatch.chart.preparation import prepare_snapshot
from nightwatch.market.data import MarketDataHub
from nightwatch.theme import THEMES


class CrosshairNavigationChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    @classmethod
    def tearDownClass(cls):
        shutdown_analysis()

    def setUp(self):
        self.chart = ChartWorkspace(THEMES['Nightwatch'], use_opengl=False)
        self.chart.resize(1000, 650)
        self.chart.show()
        self.chart.auto_scale = False
        self.chart.interval = '1m'
        self.chart.candles = [Candle(1700000000+i*60, 100, 102, 99, 101, 1000, 100000)
                              for i in range(100)]
        self.chart.candle_times = [c.time for c in self.chart.candles]
        self.view = self.chart.price_plot.getViewBox()
        self.view.setRange(xRange=(1700000000, 1700006000), yRange=(95, 105), padding=0)
        QtTest.QTest.qWait(350)
        self.viewport = self.chart.graphics.viewport()
        self.center = self.chart.graphics.mapFromScene(self.view.sceneBoundingRect().center())
        self.move(self.center)
        QtTest.QTest.qWait(30)
        self.assertTrue(self.chart.crosshair_visible)

    def tearDown(self):
        self.chart.set_presentation_active(False)
        self.chart._analysis_pool.waitForDone(2000)
        QtCore.QThreadPool.globalInstance().waitForDone(2000)
        self.app.processEvents()
        self.chart.close()

    def move(self, point, buttons=QtCore.Qt.MouseButton.NoButton):
        # Deliver input synchronously; offscreen QTest.mouseMove may suppress
        # an unchanged global position between newly created chart windows.
        event = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove, QtCore.QPointF(point),
            QtCore.QPointF(self.viewport.mapToGlobal(point)), QtCore.Qt.MouseButton.NoButton,
            buttons, QtCore.Qt.KeyboardModifier.NoModifier)
        self.app.sendEvent(self.viewport, event)

    def drag(self, point):
        QtTest.QTest.mousePress(self.viewport, QtCore.Qt.MouseButton.LeftButton, pos=self.center)
        last_dispatch = self.chart._last_crosshair_dispatch
        self.move(point, QtCore.Qt.MouseButton.LeftButton)
        QtTest.QTest.qWait(25)
        self.assertFalse(self.chart.crosshair_visible)
        self.assertEqual(self.chart._last_crosshair_dispatch, last_dispatch)

    def next_paint(self, action, *, visible=True):
        painted = []
        def record():
            painted.append(bool(self.chart.crosshair_visible))
        self.chart.graphics.frame_presented.connect(record)
        try:
            action()
            deadline = time.monotonic()+.12
            while not painted and time.monotonic() < deadline:
                QtTest.QTest.qWait(1)
        finally:
            self.chart.graphics.frame_presented.disconnect(record)
        self.assertTrue(painted, 'No chart paint followed the input')
        self.assertEqual(painted[0], visible, 'Crosshair was incorrect on the first paint')

    def release(self, point):
        QtTest.QTest.mouseRelease(self.viewport, QtCore.Qt.MouseButton.LeftButton, pos=point)

    def assert_position(self, point):
        scene = self.chart.graphics.mapToScene(point)
        expected = self.view.mapSceneToView(scene)
        self.assertAlmostEqual(self.chart.crosshair_horizontal.value(), expected.y(), places=8)
        actual_x = self.chart.crosshair_vertical.value()
        self.assertLessEqual(abs(actual_x-expected.x()), 30.)

    def test_pan_release_uses_release_position_on_first_paint(self):
        finish = self.center+QtCore.QPoint(40, 10)
        self.drag(finish)
        # The release can carry a newer position than the last mouse move.
        release = finish+QtCore.QPoint(13, 7)
        self.next_paint(lambda: self.release(release))
        self.assert_position(release)
        self.assertTrue(self.chart._interaction_priority_active)
        self.assertTrue(self.chart._navigation_is_active())

    def test_hover_and_repeat_pan_work_during_cooldown(self):
        finish = self.center+QtCore.QPoint(40, 10)
        self.drag(finish)
        self.next_paint(lambda: self.release(finish))
        hover = finish+QtCore.QPoint(60, 10)
        self.next_paint(lambda: self.move(hover))
        self.assert_position(hover)
        self.assertTrue(self.chart._interaction_priority_active)
        QtTest.QTest.mousePress(self.viewport, QtCore.Qt.MouseButton.LeftButton, pos=hover)
        self.assertFalse(self.chart.crosshair_visible)
        self.next_paint(lambda: self.release(hover))

    def test_stationary_wheel_restores_crosshair_on_first_paint(self):
        initial_range = tuple(self.view.viewRange()[0])
        wheel = QtGui.QWheelEvent(QtCore.QPointF(self.center),
            QtCore.QPointF(self.viewport.mapToGlobal(self.center)), QtCore.QPoint(), QtCore.QPoint(0, 120),
            QtCore.Qt.MouseButton.NoButton, QtCore.Qt.KeyboardModifier.NoModifier,
            QtCore.Qt.ScrollPhase.NoScrollPhase, False)
        self.next_paint(lambda: self.app.sendEvent(self.viewport, wheel))
        self.assertNotEqual(tuple(self.view.viewRange()[0]), initial_range)
        self.assert_position(self.center)

    def test_release_on_axis_does_not_restore_crosshair(self):
        self.drag(self.center+QtCore.QPoint(20, 0))
        axis = self.chart.graphics.mapFromScene(self.view.sceneBoundingRect().topRight())+QtCore.QPoint(8, 50)
        self.next_paint(lambda: self.release(axis), visible=False)
        self.assertIsNone(self.chart._pending_crosshair_position)

    def test_leaving_before_paint_cancels_restoration(self):
        finish = self.center+QtCore.QPoint(40, 10)
        self.drag(finish)
        def release_and_leave():
            self.release(finish)
            self.app.sendEvent(self.viewport, QtCore.QEvent(QtCore.QEvent.Type.Leave))
        self.next_paint(release_and_leave, visible=False)
        self.assertIsNone(self.chart._pending_crosshair_position)


class CandleHandoffChecks(unittest.TestCase):
    def setUp(self):
        self.t = 1800000000.
        self.partial = Candle(self.t, 100., 102., 99., 101., 10., 1010.)
        self.final = replace(self.partial, high=105., close=105., volume=15., quote_volume=1575.)
        self.next = Candle(self.t+60, 105., 106., 104., 106., 5., 530.)

    def hub(self, candle=None, interval='1m'):
        payload = {'symbol': 'BTCUSDT', 'interval': interval,
                   'candles': CandlePages([candle or self.partial])}
        repairs = []
        hub = SimpleNamespace(symbol='BTCUSDT', interval=interval, _live_candles={},
            chart_cache=OrderedDict({('BTCUSDT', interval): (1., payload)}),
            _mark_chart_history_repair=repairs.append)
        return hub, payload, repairs

    @staticmethod
    def event(candle, interval='1m', *, closed=False, symbol='BTCUSDT'):
        return {'s': symbol, 'E': int((candle.time+1)*1000), 'k': {
            'i': interval, 't': int(candle.time*1000), 'o': str(candle.open),
            'h': str(candle.high), 'l': str(candle.low), 'c': str(candle.close),
            'v': str(candle.volume), 'q': str(candle.quote_volume), 'x': closed}}

    def test_adjacent_resume_repairs_the_unfinished_cached_candle_and_keeps_correction(self):
        hub, cached, repairs = self.hub()
        MarketDataHub._update_chart_cache(hub, self.event(self.next))
        self.assertEqual(repairs, [self.t])
        for canonical in (False, True):
            with self.subTest(canonical=canonical):
                source = CandlePages([self.final, self.next]) if canonical else [self.final, self.next]
                result = prepare_snapshot({'interval': '1m', 'candles': source,
                                           '_canonical_candles': canonical}, cached['candles'][-2:])
                self.assertEqual(result['candles'][-2], self.final)
                self.assertEqual(result['_storage'][:2, 4].tolist(), [105., 106.])

    def test_normal_closed_rollover_and_same_bar_updates_do_not_request_repairs(self):
        hub, cached, repairs = self.hub()
        MarketDataHub._update_chart_cache(hub, self.event(self.partial))
        MarketDataHub._update_chart_cache(hub, self.event(self.final, closed=True))
        MarketDataHub._update_chart_cache(hub, self.event(self.next))
        self.assertEqual(repairs, [])
        self.assertEqual(cached['candles'][-2], self.final)

    def test_gap_repair_includes_an_uncertain_previous_bar(self):
        for closed in (False, True):
            with self.subTest(previous_closed=closed):
                hub, _cached, repairs = self.hub()
                if closed:
                    MarketDataHub._update_chart_cache(hub, self.event(self.final, closed=True))
                MarketDataHub._update_chart_cache(hub, self.event(replace(self.next, time=self.t+180)))
                self.assertEqual(repairs, [self.t+60 if closed else self.t])

    def test_calendar_rollover_and_unrelated_markets(self):
        previous = replace(self.partial, time=1767225600.)  # 2026-01-01 UTC
        hub, _cached, repairs = self.hub(previous, '1M')
        following = replace(self.next, time=shift_candle_time(previous.time, '1M'))
        MarketDataHub._update_chart_cache(hub, self.event(following, '1m'))
        MarketDataHub._update_chart_cache(hub, self.event(following, '1M', symbol='ETHUSDT'))
        self.assertFalse(hub._live_candles)
        self.assertEqual(repairs, [])
        MarketDataHub._update_chart_cache(hub, self.event(following, '1M'))
        self.assertEqual(repairs, [previous.time])

    def test_live_tail_and_newer_bars_survive_authoritative_history(self):
        live = replace(self.next, close=108., high=108., volume=8.)
        newer = replace(live, time=self.t+120, open=108., close=109., high=109.)
        for canonical in (False, True):
            with self.subTest(canonical=canonical):
                source = CandlePages([self.final, self.next]) if canonical else [self.final, self.next]
                result = prepare_snapshot({'interval': '1m', 'candles': source,
                    '_canonical_candles': canonical}, (self.partial, live, newer))
                self.assertEqual(list(result['candles']), [self.final, live, newer])

    def test_older_history_merge_and_real_exchange_price_gaps_are_preserved(self):
        gap = replace(self.next, open=105.5)
        result = prepare_snapshot({'interval': '1m', 'candles': [self.final, gap]}, (self.partial,))
        self.assertEqual(result['candles'][1].open-result['candles'][0].close, .5)
        older = replace(self.final, time=self.t-60)
        result = prepare_snapshot({'interval': '1m', '_history_merge': True,
                                   'candles': [older, self.partial]}, (self.final, self.next))
        self.assertEqual(list(result['candles']), [older, self.final, self.next])

    def test_final_confirmation_survives_a_cache_job_started_before_the_close(self):
        hub, cached, _repairs = self.hub()
        request = dict(key=('BTCUSDT', '1m'), base=cached, base_tail=self.partial,
            base_count=1, base_tail_rows={self.t: self.partial}, lifecycle=0,
            requested_at=1., timestamp=None, generation=1, publish=False, callback=None)
        hub._cache_prepare_active = request
        hub._lifecycle = 0
        hub.generation = 1
        hub.stopping = False
        hub._patch_chart_tail = MarketDataHub._patch_chart_tail
        hub._chart_cache_capacity = lambda: 8
        hub._start_cache_prepare = lambda: None
        hub._remember = lambda cache, key, payload, capacity: cache.__setitem__(key, (2., payload))
        worker_result = dict(cached, candles=cached['candles'].snapshot())
        MarketDataHub._update_chart_cache(hub, self.event(self.final, closed=True))
        cached['_repair_start'] = self.t-60
        MarketDataHub._cache_prepared(hub, 1, worker_result)
        result = hub.chart_cache[('BTCUSDT', '1m')][1]
        self.assertEqual(result['candles'][-1], self.final)
        self.assertEqual(result['_closed_kline_time'], self.t)
        self.assertEqual(result['_repair_start'], self.t-60)

    def test_fast_symbol_and_timeframe_returns_resume_a_cancelled_repair(self):
        class Timer:
            active = False
            def start(self, *_args): self.active = True
            def stop(self): self.active = False
            def isActive(self): return self.active

        hub, cached, _repairs = self.hub()
        hub.stopping = False
        hub.generation = 0
        hub.chart_only = True
        hub._chart_gap_start = None
        hub._chart_repair_revision = 0
        hub._chart_repair_attempts = 0
        hub._chart_repair_timer = Timer()
        hub._retry_attempts = {}
        hub.public_reconnect_timer = Timer()
        hub.market_reconnect_timer = Timer()
        hub.subscription_timer = Timer()
        hub._chart_prefetch_queue = []
        hub._rebuild_chart_prefetch_queue = lambda: None
        hub._market_sockets_ready = lambda: True
        hub._close_market_sockets = lambda: None
        hub._open_market_sockets = lambda _generation: None
        hub.status = SimpleNamespace(emit=lambda *_args: None)
        hub.bootstrap_ready = SimpleNamespace(emit=lambda _payload: None)
        hub._chart_snapshot = MarketDataHub._chart_snapshot
        hub._mark_chart_history_repair = MethodType(MarketDataHub._mark_chart_history_repair, hub)
        for key in (('BTCUSDT', '5m'), ('ETHUSDT', '1m')):
            hub.chart_cache[key] = (time.monotonic(), dict(cached, symbol=key[0], interval=key[1]))
        MarketDataHub._update_chart_cache(hub, self.event(self.next))
        self.assertTrue(hub._chart_repair_timer.active)
        MarketDataHub.switch_market(hub, 'BTCUSDT', '5m')
        self.assertFalse(hub._chart_repair_timer.active)
        MarketDataHub.switch_market(hub, 'ETHUSDT', '1m')
        MarketDataHub.switch_market(hub, 'BTCUSDT', '1m')
        self.assertEqual(hub._chart_gap_start, self.t)
        self.assertTrue(hub._chart_needs_repair)
        self.assertTrue(hub._chart_repair_timer.active)
        hub.connected = {'market': True}
        hub._run = lambda fetch, loaded, *args, **kwargs: loaded({'candles': [self.final, self.next]})
        def publish(key, payload, **options):
            cached.update(payload)
            self.assertIn('_repair_start', cached)
            options['callback']()
        hub._queue_chart_cache = publish
        MarketDataHub._repair_chart_history(hub)
        self.assertNotIn('_repair_start', cached)
        self.assertFalse(hub._chart_needs_repair)
        self.assertFalse(hub._chart_repair_timer.active)


if __name__ == '__main__':
    unittest.main()
