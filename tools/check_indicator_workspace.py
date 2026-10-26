#!/usr/bin/env python3
"""Offline chart checks for indicator lifecycle, live updates, and pane scaling."""
from __future__ import annotations

import math
import os
from pathlib import Path
import sys
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from nightwatch.chart.analysis import shutdown_analysis
from nightwatch.chart.workspace import ChartWorkspace
from nightwatch.chart.rendering import PreparedCurveItem
from nightwatch.constants import INDICATOR_KEYS, INDICATOR_SETTING_DEFAULTS
from nightwatch.models import Candle
from nightwatch.theme import THEMES


def candles(count=800):
    start = 1_783_296_000.0  # UTC midnight, so the first VWAP anchor is complete.
    result = []
    for index in range(count):
        close = 100 + index * 0.025 + 5 * math.sin(index / 19)
        opening = close + math.cos(index / 13)
        volume = 100 + index % 29 * 7
        result.append(Candle(
            start + index * 900, opening, max(opening, close) + 1,
            min(opening, close) - 1, close, volume, volume * close,
        ))
    return result


class IndicatorWorkspaceChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.app.setStyle("Fusion")
        cls.qt_errors = []
        cls.previous_hook = sys.excepthook

        def record_exception(kind, value, traceback):
            cls.qt_errors.append(value)
            cls.previous_hook(kind, value, traceback)

        sys.excepthook = record_exception

    @classmethod
    def tearDownClass(cls):
        shutdown_analysis()
        sys.excepthook = cls.previous_hook

    def wait_until(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        self.assertTrue(predicate(), "Timed out waiting for the production chart pipeline")
        self.assertEqual(self.qt_errors, [])

    def settle(self):
        for _ in range(8):
            self.app.processEvents()

    def setUp(self):
        self.qt_errors.clear()
        self.chart = ChartWorkspace(THEMES["Nightwatch"], use_opengl=False)
        self.chart.resize(1320, 1020)
        self.chart.show()
        self.source = candles()
        self.chart.set_snapshot({
            "interval": "15m", "candles": self.source,
            "oi_history": [], "_history_exhausted": True,
        })
        self.wait_until(lambda: self.chart._snapshot_loaded)
        self.settle()

    def tearDown(self):
        self.chart.set_indicators_enabled(False)
        self.chart._indicator_pool.waitForDone(15000)
        self.chart._analysis_pool.waitForDone(15000)
        self.chart._render_pool.waitForDone(15000)
        self.chart._preparation_pool.waitForDone(15000)
        self.chart.close()
        self.chart.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.settle()

    def enable_computed(self):
        names = (
            "EMA Trend", "VWAP", "Supertrend", "Bollinger Bands",
            "RSI", "ADX / DI", "Relative Volume", "Chaikin Money Flow",
        )
        for name in names:
            self.chart.toggle_indicator(name, True)
        self.wait_until(lambda: self.chart._indicator_committed_key is not None
                        and self.chart._indicator_display_async_key is None
                        and self.chart._indicator_display_async_pending is None
                        and self.chart._indicator_display_mailbox is None)
        self.wait_until(lambda: all(
            curve.getData()[1] is not None and len(curve.getData()[1])
            for curves in self.chart.candle_study_curves.values()
            for curve in curves
        ))

    def test_catalog_toggle_layout_crosshair_and_theme(self):
        self.assertEqual(set(self.chart.indicators), set(INDICATOR_KEYS))
        retired = {"ATR", "Donchian Channels", "Session Volume Profile", "Major Price Levels"}
        self.assertFalse(retired & set(self.chart.indicators))
        self.assertFalse(any(self.chart.indicators.values()))
        self.assertTrue(all(not plot.isVisible() for plot in self.chart.study_plots.values()))
        self.enable_computed()
        for name in INDICATOR_KEYS:
            self.chart.toggle_indicator(name, True)
        self.settle()
        self.assertTrue(all(plot.isVisible() for plot in self.chart.study_plots.values()))
        self.chart._set_crosshair_visible(True)
        self.assertTrue(all(line.isVisible() for line in self.chart.crosshair_study_verticals))
        plots = [self.chart.price_plot, *self.chart.study_plots.values()]
        owners = [plot for plot in plots if plot.getAxis("bottom").isVisible()]
        self.assertEqual(len(owners), 1)
        self.assertIs(owners[0], list(self.chart.study_plots.values())[-1])
        for theme_name in ("Paper Desk", "Deep Ocean", "Nightwatch"):
            self.chart.apply_theme(THEMES[theme_name])
            self.settle()
        self.chart.set_indicators_enabled(False)
        self.settle()
        self.assertTrue(all(not plot.isVisible() for plot in self.chart.study_plots.values()))
        self.assertTrue(all(not line.isVisible() for line in self.chart.crosshair_study_verticals))
        self.assertTrue(self.chart.price_plot.getAxis("bottom").isVisible())
        self.chart.set_indicators_enabled(True)
        self.settle()
        for name in tuple(self.chart.study_plots):
            self.chart._remove_study(name)
        self.assertTrue(all(not plot.isVisible() for plot in self.chart.study_plots.values()))
        self.assertTrue(self.chart.price_plot.getAxis("bottom").isVisible())
        self.assertEqual(self.qt_errors, [])

    def test_live_updates_and_log_price_keep_study_units(self):
        self.enable_computed()
        original = {
            name: tuple(curve.getData()[1].copy() for curve in curves)
            for name, curves in self.chart.candle_study_curves.items()
        }
        previous = self.chart._indicator_committed_key
        self.chart.set_logarithmic(not self.chart.logarithmic)
        self.wait_until(lambda: self.chart._indicator_committed_key is not None
                        and self.chart._indicator_committed_key != previous
                        and self.chart._indicator_display_async_key is None)
        for name, curves in self.chart.candle_study_curves.items():
            for expected, curve in zip(original[name], curves):
                np.testing.assert_allclose(curve.getData()[1], expected, equal_nan=True)

        last = self.source[-1]
        previous = self.chart._indicator_committed_key
        previous_rvol = self.chart.candle_study_curves["Relative Volume"][0].getData()[1][-1]
        self.chart.update_kline({
            "E": (last.time + 899) * 1000,
            "k": {"t": last.time * 1000, "i": "15m", "o": str(last.open),
                  "h": str(last.high + 4), "l": str(last.low), "c": str(last.high + 3),
                  "v": str(last.volume * 3), "q": str(last.quote_volume * 3), "x": False},
        })
        self.wait_until(lambda: self.chart._indicator_committed_key is not None
                        and self.chart._indicator_committed_key != previous
                        and self.chart._indicator_display_async_key is None)
        new_rvol = self.chart.candle_study_curves["Relative Volume"][0].getData()[1][-1]
        self.assertAlmostEqual(new_rvol, previous_rvol * 3, places=9)
        for name in ("RSI", "ADX / DI"):
            for curve in self.chart.candle_study_curves[name]:
                values = curve.getData()[1]
                values = values[np.isfinite(values)]
                self.assertTrue(np.all((values >= 0) & (values <= 100)))
        cmf = self.chart.candle_study_curves["Chaikin Money Flow"][0].getData()[1]
        self.assertTrue(np.all(np.abs(cmf[np.isfinite(cmf)]) <= 1))

    def test_settings_refresh_and_manual_scale_reset(self):
        self.enable_computed()
        settings = {name: dict(values) for name, values in INDICATOR_SETTING_DEFAULTS.items()}
        settings["Relative Volume"]["period"] = 7
        settings["ADX / DI"]["threshold"] = 30
        settings["Supertrend"]["multiplier"] = 1.5
        previous = self.chart._indicator_committed_key
        self.chart.set_indicator_settings(settings)
        self.wait_until(lambda: self.chart._indicator_committed_key is not None
                        and self.chart._indicator_committed_key != previous
                        and self.chart._indicator_display_async_key is None)
        expected = self.source[-1].volume / np.mean([c.volume for c in self.source[-8:-1]])
        actual = self.chart.candle_study_curves["Relative Volume"][0].getData()[1][-1]
        self.assertAlmostEqual(actual, expected, places=9)
        for name in ("RSI", "ADX / DI", "Relative Volume", "Chaikin Money Flow"):
            self.chart._set_study_manual_scale(name)
            self.assertTrue(self.chart.study_manual_scale[name])
            self.chart._reset_study_scale(name)
            self.assertFalse(self.chart.study_manual_scale[name])
        self.chart.set_study_heights({"ADX / DI": 180, "Relative Volume": 160})
        self.assertEqual(self.chart.saved_study_heights()["ADX / DI"], 180)
        self.assertEqual(self.qt_errors, [])


class PreparedCurveChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.widget = pg.PlotWidget()
        if os.environ.get("NIGHTWATCH_TEST_GL") == "1":
            self.widget.useOpenGL(True)
        self.widget.resize(800, 500)
        self.plot = self.widget.getPlotItem()
        self.plot.disableAutoRange()
        self.plot.setXRange(20, 120, padding=0)
        self.plot.setYRange(-2, 2, padding=0)
        self.curve = PreparedCurveItem(self.plot, pen=pg.mkPen("#22dddd", width=1), antialias=True)
        self.x = np.linspace(0, 200, 2000)
        self.y = np.sin(self.x / 5)
        self.y[500:650] = np.nan
        self.curve.setData(self.x, self.y, connect="finite")
        self.widget.show()
        self.app.processEvents()

    def tearDown(self):
        self.widget.close()
        self.widget.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_camera_reuses_path_but_data_and_styles_refresh(self):
        submissions = []
        self.curve.curve.sigPlotChanged.connect(lambda *_: submissions.append(True))
        path = self.curve.curve.getPath()
        gl_state = self.curve.curve.glstate
        if os.environ.get("NIGHTWATCH_TEST_GL") == "1":
            self.assertTrue(self.widget.viewport().isValid())
            self.assertIsNotNone(gl_state)
            self.assertIsNotNone(gl_state.render_cache)
        gl_cache = gl_state.render_cache if gl_state is not None else None
        for step in range(20):
            self.plot.setXRange(20 + step, 120 + step, padding=0)
            self.plot.setYRange(-2 + step*.01, 2 + step*.01, padding=0)
            self.app.processEvents()
            self.assertIs(self.curve.curve.getPath(), path)
            if gl_state is not None:
                self.assertIs(gl_state.render_cache, gl_cache)
        self.assertEqual(submissions, [])
        np.testing.assert_allclose(self.curve.getData()[1], self.y, equal_nan=True)

        changed = self.y.copy()
        changed[-1] = 1.5
        self.curve.setData(self.x, changed, connect="finite")
        self.assertTrue(submissions)
        self.assertIsNot(self.curve.curve.getPath(), path)
        self.assertEqual(self.curve.getData()[1][-1], 1.5)
        self.curve.setPen(pg.mkPen("#ff9900", width=1))
        self.assertEqual(self.curve.curve.opts["pen"].color(), QtGui.QColor("#ff9900"))
        self.curve.clear()
        self.curve.setData(self.x, self.y, connect="finite")
        np.testing.assert_allclose(self.curve.getData()[1], self.y, equal_nan=True)

    def test_clipped_curve_reuses_y_changes_and_refreshes_x_window(self):
        self.curve.setClipToView(True)
        submissions = []
        self.curve.curve.sigPlotChanged.connect(lambda *_: submissions.append(True))
        path = self.curve.curve.getPath()
        original_x = self.curve.getData()[0].copy()
        self.assertLess(len(original_x), len(self.x))
        for step in range(20):
            self.plot.setYRange(-2 + step*.01, 2 + step*.01, padding=0)
            self.app.processEvents()
            self.assertIs(self.curve.curve.getPath(), path)
        self.assertEqual(submissions, [])
        self.plot.setXRange(30, 130, padding=0)
        self.app.processEvents()
        self.assertEqual(submissions, [])
        self.assertIs(self.curve.curve.getPath(), path)
        self.plot.setXRange(65, 165, padding=0)
        self.app.processEvents()
        self.assertTrue(submissions)
        self.assertGreater(self.curve.getData()[0][0], original_x[0])
        self.assertIsNot(self.curve.curve.getPath(), path)

    def test_clipped_pan_margin_matches_reference_pixels(self):
        self.curve.setClipToView(True)
        reference = self.plot.plot(pen=pg.mkPen("#22dddd", width=1), antialias=True)
        reference.setClipToView(True)
        reference.setData(self.x, self.y, connect="finite")
        reference.hide()
        for curve in (self.curve, reference):
            curve.setDynamicRangeLimit(10, hysteresis=3)
        for left, width, bounds in (
            (20, 100, (-2, 2)), (30, 100, (-2, 2)),
            (65, 100, (-2, 2)), (105, 20, (-2, 2)),
            (50, 100, (-.001, .001)), (60, 100, (-.002, .002)),
            (-40, 100, (-2, 2)), (180, 100, (-2, 2)),
        ):
            self.plot.setXRange(left, left + width, padding=0)
            self.plot.setYRange(*bounds, padding=0)
            self.curve.show()
            reference.hide()
            for _ in range(8):
                self.app.processEvents()
            viewport = self.widget.viewport()
            actual = viewport.grabFramebuffer() if hasattr(viewport, "grabFramebuffer") else self.widget.grab().toImage()
            self.curve.hide()
            reference.show()
            for _ in range(8):
                self.app.processEvents()
            expected = viewport.grabFramebuffer() if hasattr(viewport, "grabFramebuffer") else self.widget.grab().toImage()
            # A retained segment continues beyond the viewport instead of
            # ending at the upstream clip point. Compare the complete interior
            # while excluding the edge pixels affected by cap antialiasing.
            area = self.widget.mapFromScene(self.plot.getViewBox().sceneBoundingRect()).boundingRect()
            area = area.adjusted(4, 4, -4, -4)
            self.assertEqual(actual.copy(area), expected.copy(area),
                             f"Clipped curve changed at {left, width, bounds}")

    def test_explicit_data_updates_invalidate_reused_array_storage(self):
        path = self.curve.curve.getPath()
        self.y[-1] = 1.5
        self.curve.setData(self.x, self.y)
        self.assertIsNot(self.curve.curve.getPath(), path)
        self.assertEqual(self.curve.curve.getData()[1][-1], 1.5)
        self.curve.clear()
        self.curve.setData(self.x, self.y)
        self.assertFalse(self.curve.curve.getPath().isEmpty())
        self.assertTrue(self.curve.curve.isVisible())

    def test_rendering_matches_reference_including_extreme_scale_and_gaps(self):
        reference = self.plot.plot(pen=pg.mkPen("#22dddd", width=1), antialias=True)
        reference.setClipToView(False)
        reference.setDownsampling(ds=1, auto=False)
        reference.setData(self.x, self.y, connect="finite")
        reference.hide()
        # Exercise entry into and recovery from pyqtgraph's safety clipping.
        for curve in (self.curve, reference):
            curve.setDynamicRangeLimit(10, hysteresis=3)
        for bounds in ((-2, 2), (-.001, .001), (-.002, .002), (-2, 2)):
            self.plot.setYRange(*bounds, padding=0)
            self.curve.show()
            reference.hide()
            for _ in range(8):
                self.app.processEvents()
            viewport = self.widget.viewport()
            actual = viewport.grabFramebuffer() if hasattr(viewport, "grabFramebuffer") else self.widget.grab().toImage()
            self.curve.hide()
            reference.show()
            for _ in range(8):
                self.app.processEvents()
            expected = viewport.grabFramebuffer() if hasattr(viewport, "grabFramebuffer") else self.widget.grab().toImage()
            np.testing.assert_allclose(self.curve.getData()[1], reference.getData()[1], equal_nan=True)
            self.assertEqual(actual, expected, f"Raster changed at Y range {bounds}")
        self.assertTrue(np.isnan(self.curve.getData()[1][500:650]).all())


if __name__ == "__main__":
    unittest.main()
