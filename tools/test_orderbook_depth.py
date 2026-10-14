"""Offline checks for grouped depth and selection of actually displayed prices."""
from __future__ import annotations

from dataclasses import replace
import os
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
    _DepthAggregation, _fit_depth_geometry, _geometry, PrismCanvas,
)
from nightwatch.orderbook.orderbook_ui import aggregate_order_flow_snapshot
from nightwatch.theme import orderbook_palette


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


class _LocalLink(QtCore.QObject):
    ready = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, *args, **kwargs):
        super().__init__()

    def submit(self, *args, **kwargs): pass
    def enable(self, *args, **kwargs): pass
    def close(self): pass
    def consumed(self, *args, **kwargs): pass


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
