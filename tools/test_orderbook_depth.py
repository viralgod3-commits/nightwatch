"""Offline checks for grouped depth and selection of actually displayed prices."""
from __future__ import annotations

from dataclasses import replace
import json
import os
import pickle
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from nightwatch.models import (
    ORDER_FLOW_AGGREGATION_MULTIPLIERS, OrderFlowComponentRevisions,
    OrderFlowDisplayLevel, OrderFlowSnapshot,
)
from nightwatch.orderbook.modern import (
    _DepthAggregation, _fit_depth_geometry, _geometry, _PrismRenderer, PrismCanvas, OrderBookWidget,
)
from nightwatch.market.data import _DepthParserWorker
from nightwatch.orderbook.backend import OrderFlowAnalyzer
from nightwatch.orderbook.ipc import SnapshotDecoder, SnapshotEncoder
from nightwatch.orderbook.orderbook_ui import aggregate_order_flow_snapshot
from nightwatch.theme import orderbook_palette
from nightwatch.utilities import TYPOGRAPHY_DEFAULTS, typography_controller


def snapshot(tick=.1, midpoint=60000., count=96):
    def side(name, direction):
        result = []
        for i in range(count):
            price = midpoint + direction * (i+1) * tick
            quantity = .5 + i % 7
            result.append(OrderFlowDisplayLevel(name, price, quantity, price*quantity,
                0., 0., 0., 0., 1., .5, 0, 'NORMAL', .5, 0., 0.,
                liquidity_history_30s=(.2, .5, .8), history_peak_notional_30s=price*8))
        return tuple(result)
    return OrderFlowSnapshot('BTCUSDT', 1, time.monotonic(), True, True, 'depth',
        0., 0., 0., best_bid=midpoint-tick, best_ask=midpoint+tick,
        midpoint=midpoint, spread=2*tick,
        bid_levels=side('bid', -1), ask_levels=side('ask', 1))


class DepthChecks(unittest.TestCase):
    def test_grouped_prices_and_amounts_match_canonical_aggregation(self):
        for tick, midpoint in ((.1, 60000.), (.000001, .012345), (2.5, 12345.)):
            source = snapshot(tick, midpoint)
            aggregation = _DepthAggregation()
            for multiplier in ORDER_FLOW_AGGREGATION_MULTIPLIERS:
                reference = aggregate_order_flow_snapshot(source, multiplier, tick)
                aggregation.prepare(source, multiplier, tick, 0)
                for expected, actual in zip((reference.ask_levels, reference.bid_levels), aggregation.levels):
                    with self.subTest(tick=tick, multiplier=multiplier):
                        self.assertEqual([(l.price, l.quantity, l.notional) for l in expected],
                                         [(l.price, l.quantity, l.notional) for l in actual])

    def test_analytics_only_updates_reuse_buckets_and_amount_changes_rebuild(self):
        aggregation = _DepthAggregation()
        source = replace(snapshot(), component_revisions=OrderFlowComponentRevisions('first', 1, 1, 1, 1))
        aggregation.prepare(source, 10, .1, 0)
        original = aggregation.levels
        analytics = replace(source, sequence=2, buy_notional_5s=5000.,
            bid_levels=tuple(replace(l, age_seconds=2., buy_trade_notional_5s=5000.) for l in source.bid_levels),
            component_revisions=replace(source.component_revisions, bid_levels=2, recent_prints=2))
        aggregation.prepare(analytics, 10, .1, 0)
        self.assertIs(aggregation.levels, original)
        changed = replace(analytics, bid_levels=(replace(analytics.bid_levels[0], quantity=42., notional=42.*59999.9),
            *analytics.bid_levels[1:]), component_revisions=replace(analytics.component_revisions, amounts=2))
        aggregation.prepare(changed, 10, .1, 0)
        self.assertNotEqual(aggregation.levels, original)
        previous = aggregation.levels
        aggregation.prepare(replace(changed, component_revisions=replace(changed.component_revisions, stream='reset')), 10, .1, 0)
        self.assertIsNot(aggregation.levels, previous)
        aggregation.prepare(replace(changed, ready=False), 10, .1, 0)
        self.assertEqual(aggregation.levels, ((), ()))

    def test_unversioned_snapshots_compare_actual_amounts(self):
        aggregation = _DepthAggregation()
        source = snapshot()
        aggregation.prepare(source, 10, .1, 0)
        original = aggregation.levels
        aggregation.prepare(replace(source, bid_levels=tuple(replace(l, age_seconds=2.) for l in source.bid_levels)), 10, .1, 0)
        self.assertIs(aggregation.levels, original)
        changed = replace(source, bid_levels=(replace(source.bid_levels[0], quantity=2., notional=119999.8), *source.bid_levels[1:]))
        aggregation.prepare(changed, 10, .1, 0)
        self.assertNotEqual(aggregation.levels, original)
        aggregation.prepare(source, 1, .1, 0)
        self.assertIs(aggregation.levels[1], source.bid_levels)


class DeepFeedChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def seed(self, symbol='ETHUSDT', midpoint=2412.95, tick=.01, multiplier=250):
        self.worker = _DepthParserWorker()
        self.events, self.invalidations = [], []
        self.worker.parsed.connect(lambda _epoch, _revision, event: self.events.append(event))
        self.worker.invalidated.connect(lambda *_args: self.invalidations.append(_args))
        self.worker.reset_epoch(1, 0, symbol)
        self.worker.set_display_depth(symbol, 24, multiplier, tick)
        native = round(midpoint / tick)
        self.worker.seed_snapshot(1, 0, dict(lastUpdateId=100,
            bids=[[(native-i-1)*tick, 1.] for i in range(1000)],
            asks=[[(native+i+1)*tick, 2.] for i in range(1000)]))
        self.symbol, self.tick = symbol, tick
        self.send([], [], first=True)

    def send(self, bids, asks, *, first=False, gap=False):
        previous = self.worker._last_update_id
        final = previous if first else previous+1
        self.worker.parse(1, json.dumps(dict(e='depthUpdate', s=self.symbol,
            U=final, u=final, pu=previous-1 if first or gap else previous,
            b=bids, a=asks)), self.symbol, ())
        return self.events[-1] if self.events else None

    def test_deeper_updates_reach_renderer_without_expanding_analytics(self):
        self.seed()
        event = self.send([[2390., 5.], [2385., 7.]], [[2430., 9.], [2435., 11.]])
        self.assertEqual(len(event['_analysis_bids']), 120)
        self.assertEqual(len(event['_analysis_asks']), 120)
        view = event['_display_depth']
        self.assertEqual(view.bids[-1], (2385., 7., 16695., False))
        self.assertEqual(view.asks[-1], (2435., 11., 26785., False))
        analyzer = OrderFlowAnalyzer(self.symbol)
        analyzer.reset(self.symbol, tick_size=self.tick)
        analyzer.add_depth(event['_analysis_bids'], event['_analysis_asks'],
            source_revision=event['_analysis_revision'], display_depth=view)
        source = analyzer.snapshot(limit_per_side=120)
        wire = SnapshotEncoder().encode(source, 1)
        restored = SnapshotDecoder().decode(pickle.loads(pickle.dumps(wire)), 1)
        self.assertEqual(restored.display_depth, view)
        renderer = _PrismRenderer(orderbook_palette())
        controller = typography_controller()
        renderer.configure(dict(symbol=self.symbol, market_epoch=1, size=(408, 720), dpr=1.,
            aggregation=250, tick=self.tick, density='comfortable', values='quote', totals=True,
            motion=False, theme=orderbook_palette(),
            typography=({role: controller.profile(role) for role in TYPOGRAPHY_DEFAULTS}, controller.globals())))
        renderer.adopt(restored)
        renderer.prepare(time.monotonic())
        self.assertTrue(any(row.price < 2400 for row in renderer.rows))
        self.assertTrue(any(row.price > 2425 for row in renderer.rows))
        self.assertTrue(next(row for row in renderer.rows if row.price == 2385).size_text.startswith('≥'))
        renderer.close()

    def test_growth_no_longer_rebases_and_retention_remains_bounded(self):
        self.seed()
        event = self.send([[2402.-i*.01, 1.] for i in range(5000)],
                          [[2423.+i*.01, 2.] for i in range(5000)])
        self.assertFalse(self.invalidations)
        self.assertTrue(self.worker._ready)
        self.assertLess(event['_display_depth'].bids[-1][0], 2400)
        self.assertGreater(event['_display_depth'].asks[-1][0], 2425)
        best = (self.worker._bid_prices[-1], self.worker._ask_prices[0])
        self.send([[2300.-i*.01, 1.] for i in range(18000)],
                  [[2500.+i*.01, 2.] for i in range(18000)])
        self.assertFalse(self.invalidations)
        self.assertEqual((len(self.worker._bids), len(self.worker._asks)), (12288, 12288))
        self.assertEqual(best, (self.worker._bid_prices[-1], self.worker._ask_prices[0]))
        self.assertLessEqual(len(self.events[-1]['_display_depth'].bids), 24)
        for side, book in (('bid', self.worker._bids), ('ask', self.worker._asks)):
            buckets = self.worker._display_buckets[side]
            self.assertEqual(sum(v[2] for v in buckets.values()), len(book))
            self.assertAlmostEqual(sum(v[0] for v in buckets.values()), sum(book.values()))

    def test_quantity_replacements_deletions_and_quote_sums(self):
        self.seed()
        self.send([[2390.01, 5.], [2390.02, 3.]], [[2430.01, 4.]])
        event = self.send([[2390.01, 2.], [2390.02, 0.]], [[2430.01, 0.]])
        self.assertEqual(event['_display_depth'].bids[-1][:3], (2390., 2., 4780.02))
        self.assertNotIn(2432.5, [row[0] for row in event['_display_depth'].asks])

    def test_disabled_view_still_trims_book_and_reenables_from_current_state(self):
        self.seed()
        self.worker.set_display_depth(self.symbol, 0, 250, self.tick)
        self.send([[2300.-i*.01, 1.] for i in range(18000)], [])
        self.assertEqual(len(self.worker._bids), len(self.worker._bid_prices))
        self.assertLessEqual(len(self.worker._bids), self.worker.MAX_RETAINED_LEVELS)
        self.worker.set_display_depth(self.symbol, 24, 250, self.tick)
        event = self.send([], [])
        self.assertLess(event['_display_depth'].bids[-1][0], 2400.)
        self.assertEqual(sum(v[2] for v in self.worker._display_buckets['bid'].values()),
                         len(self.worker._bids))

    def test_far_changes_reuse_visible_view_and_grouping_uses_retained_book(self):
        self.seed()
        self.worker.set_display_depth(self.symbol, 3, 250, self.tick)
        event = self.send([], [])
        old = event['_display_depth']
        event = self.send([[2300., 10.]], [[2500., 10.]])
        self.assertIs(event['_display_depth'], old)
        self.worker.set_display_depth(self.symbol, 24, 250, self.tick)
        event = self.send([], [])
        self.assertEqual(event['_display_depth'].bids[-1][0], 2300.)
        self.worker.set_display_depth(self.symbol, 24, 1000, self.tick)
        event = self.send([], [])
        self.assertEqual(event['_display_depth'].bids[-1][0], 2300.)
        self.assertEqual(event['_display_depth'].asks[-1][0], 2500.)

    def test_native_view_reads_only_visible_rows_without_a_second_book(self):
        self.seed(multiplier=1)
        old = self.events[-1]['_display_depth']
        self.assertEqual(len(old.bids), 24)
        self.assertFalse(self.worker._display_buckets['bid'])
        self.assertIs(self.send([[2300., 2.]], [])['_display_depth'], old)
        best = old.bids[0][0]
        event = self.send([[best, 4.]], [])
        self.assertEqual(event['_display_depth'].bids[0], (best, 4., best*4., True))
        self.worker.set_display_depth(self.symbol, 24, 250, self.tick)
        event = self.send([], [])
        self.assertEqual(event['_display_depth'].bids[-1][0], 2300.)

    def test_small_tick_symbols_and_market_reset(self):
        for symbol, midpoint, tick in (('SMALLUSDT', .012345, .000001), ('BTCUSDT', 60000., .1)):
            with self.subTest(symbol=symbol):
                self.seed(symbol, midpoint, tick, 250)
                low, high = midpoint-2500*tick, midpoint+2500*tick
                event = self.send([[low, 10.]], [[high, 20.]])
                self.assertLess(event['_display_depth'].bids[-1][0], midpoint-1000*tick)
                self.assertGreater(event['_display_depth'].asks[-1][0], midpoint+1000*tick)
                self.send([], [], gap=True)
                self.assertTrue(self.invalidations)
                self.assertIsNone(self.worker._display_view)
                self.assertFalse(self.worker._display_buckets['bid'])
                self.worker.reset_epoch(2, 0, 'OTHERUSDT')
                self.assertIsNone(self.worker._visible_depth())


class _LocalLink(QtCore.QObject):
    ready = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, *args, **kwargs):
        super().__init__()

    def submit(self, *args, **kwargs): pass
    def enable(self, *args, **kwargs): pass
    def close(self): pass
    def consumed(self, *args, **kwargs): pass


class HeatmapChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        with patch('nightwatch.orderbook.backend._OrderBookProcessLink', _LocalLink):
            self.widget = OrderBookWidget(orderbook_palette())
        self.widget.set_symbol('BTCUSDT', SimpleNamespace(tick_size=.1))
        self.widget.resize(360, 600)
        self.widget.show()
        self.app.processEvents()

    def tearDown(self):
        self.widget.close()

    def test_visible_switch_range_and_saved_state_without_new_depth_requests(self):
        w = self.widget
        requests, changes = [], []
        w.depth_view_requested.connect(lambda *args: requests.append(args))
        w.presentation_changed.connect(changes.append)
        QtTest.QTest.mouseClick(w.heatmap_button, QtCore.Qt.MouseButton.LeftButton)
        self.assertTrue(w.canvas._sent_config['heatmap'])
        self.assertTrue(w._range_action.isVisible())
        w.depth_range_slider.setValue(35)
        saved = w.presentation_state()
        self.assertEqual(saved['depth_range'], .35)
        self.assertEqual(w.canvas._sent_config['depth_range'], .35)
        self.assertEqual(requests, [])
        self.assertEqual(len(changes), 2)
        w.restore_presentation_state({})
        self.assertFalse(w.heatmap_button.isChecked())
        self.assertFalse(w._range_action.isVisible())
        w.restore_presentation_state(saved)
        self.assertTrue(w.heatmap_button.isChecked())
        self.assertEqual(w.depth_range_slider.value(), 35)
        w.depth_range_slider.setValue(100)
        self.assertEqual(w.canvas.depth_range, 1.)
        w.depth_range_slider.setValue(5)
        self.assertEqual(w.canvas.depth_range, .05)

    def test_long_symbols_do_not_widen_or_overlap_narrow_panels(self):
        w = self.widget
        w.set_symbol('1000PEPEUSDT')
        w.set_aggregation_multiplier(10000)
        for width in (220, 260, 320, 600, 220):
            with self.subTest(width=width):
                w.resize(width, 520)
                self.app.processEvents()
                self.app.processEvents()
                self.assertEqual(w.width(), width)
                bounds = []
                for button in (w.grouping, w.heatmap_button, w.unit, w.options):
                    rect = QtCore.QRect(button.mapTo(w.controls, QtCore.QPoint()), button.size())
                    self.assertTrue(w.controls.rect().contains(rect))
                    self.assertGreaterEqual(button.width(), button.sizeHint().width())
                    self.assertFalse(any(rect.intersects(previous) for previous in bounds))
                    bounds.append(rect)

    def test_shared_cumulative_scale_preserves_prices_sizes_and_idle_rendering(self):
        w = self.widget
        source = snapshot(count=24)
        source = replace(source,
            bid_levels=tuple(replace(level, quantity=3., notional=3.*level.price) for level in source.bid_levels),
            ask_levels=tuple(replace(level, quantity=12., notional=12.*level.price) for level in source.ask_levels))
        renderer = _PrismRenderer(orderbook_palette())
        now = source.generated_monotonic
        renderer.configure(w.canvas._sent_config)
        renderer.adopt(source)
        renderer.prepare(now)
        original = [(row.side, row.price, row.size_text, row.total_text) for row in renderer.rows]
        grouped = renderer._aggregation.levels
        w.set_heatmap_enabled(True)
        renderer.configure(w.canvas._sent_config)
        renderer.prepare(now)
        self.assertTrue(all(current is old for current, old in zip(renderer._aggregation.levels, grouped)))
        self.assertEqual([(r.side, r.price, r.size_text, r.total_text) for r in renderer.rows], original)
        self.assertEqual(max(r.depth_ratio for r in renderer.rows if r.side == 'ask'), 1.)
        self.assertEqual(max(r.depth_ratio for r in renderer.rows if r.side == 'bid'), .25)
        totals = renderer.side_totals
        w.set_depth_range(.5)
        renderer.configure(w.canvas._sent_config)
        renderer.prepare(now)
        self.assertEqual([(r.side, r.price, r.size_text, r.total_text) for r in renderer.rows], original)
        self.assertEqual(renderer.side_totals, totals)
        self.assertGreater(max(r.depth_ratio for r in renderer.rows if r.side == 'bid'), .25)
        target = QtGui.QImage(renderer.size(), QtGui.QImage.Format_ARGB32_Premultiplied)
        renderer.paint(target, QtGui.QRegion(renderer.rect()), now)
        brushes = dict(renderer._depth_brushes)
        renderer._dirty_pixels = QtGui.QRegion()
        renderer.prepare(now+.1)
        self.assertTrue(renderer._dirty_pixels.isEmpty())
        self.assertLessEqual(renderer._animation_end, now)
        self.assertEqual(renderer._depth_brushes, brushes)
        renderer.close()


class PriceSelectionChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        with patch('nightwatch.orderbook.backend._OrderBookProcessLink', _LocalLink):
            self.canvas = PrismCanvas(orderbook_palette())
        self.canvas.resize(400, 500)
        self.canvas.show()
        self.app.processEvents()
        self.geometry = _fit_depth_geometry(_geometry(400, 500, 'compact', 84, False), 4, 4, grouped=False)
        self.point = QtCore.QPoint(200, self.geometry['center_bottom'] + 10)
        self.selections = []
        self.canvas.price_selected.connect(self.selections.append)

    def tearDown(self):
        self.canvas.close()

    def deliver(self, price=59999.9, revision=1, dirty_rects=((0, 0, 400, 500),)):
        image = QtGui.QImage(400, 500, QtGui.QImage.Format_ARGB32_Premultiplied)
        image.fill(QtCore.Qt.GlobalColor.black)
        frame = dict(frame=('fixture', 0, 400, 500, 1.), frame_revision=revision,
            frame_base=self.canvas._frame_lease(self.canvas._display_frame),
            epoch=self.canvas._display_epoch, market_epoch=self.canvas._market_epoch,
            geometry=self.geometry, prices=((60000.1, 60000.2, 60000.3, 60000.4),
                (price, 59999.8, 59999.7, 59999.6)), prices_valid_until=time.monotonic()+1,
            sequence=revision, generated_monotonic=time.monotonic(), status='LIVE',
            pixels=SimpleNamespace(images=[image]), dirty_rects=dirty_rects)
        self.canvas._commit_raster(frame)

    def click(self):
        QtTest.QTest.mouseClick(self.canvas, QtCore.Qt.MouseButton.LeftButton, pos=self.point)

    def test_new_price_is_selectable_only_after_paint(self):
        self.deliver()
        self.assertIsNone(self.canvas._hit(self.point))
        self.app.processEvents()
        self.click()
        self.assertEqual(self.selections, [59999.9])
        self.deliver(59999.95, 2, ((0, self.geometry['center_bottom'], 400, 20),))
        ask = QtCore.QPoint(200, self.geometry['center_top']-10)
        self.assertIsNotNone(self.canvas._hit(ask))
        self.click()
        self.assertEqual(self.selections, [59999.9])
        self.app.processEvents()
        self.click()
        self.assertEqual(self.selections, [59999.9, 59999.95])

    def test_size_updates_keep_existing_prices_clickable(self):
        self.deliver()
        self.app.processEvents()
        self.deliver(revision=2)
        self.assertFalse(self.canvas._unpainted_region.isEmpty())
        self.click()
        self.assertEqual(self.selections, [59999.9])

    def test_changed_and_stale_prices_cancel_a_pending_click(self):
        self.deliver()
        self.app.processEvents()
        QtTest.QTest.mousePress(self.canvas, QtCore.Qt.MouseButton.LeftButton, pos=self.point)
        self.deliver(59999.95, 2)
        self.app.processEvents()
        QtTest.QTest.mouseRelease(self.canvas, QtCore.Qt.MouseButton.LeftButton, pos=self.point)
        self.assertEqual(self.selections, [])
        self.canvas._display_frame['prices_valid_until'] = time.monotonic()-1
        self.click()
        self.assertEqual(self.selections, [])


if __name__ == '__main__':
    unittest.main()
