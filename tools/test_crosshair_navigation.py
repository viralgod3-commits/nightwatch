"""Exercise crosshair restoration through the real Qt chart input/paint path."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import time
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from nightwatch.chart.analysis import shutdown_analysis
from nightwatch.chart.workspace import ChartWorkspace
from nightwatch.models import Candle
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


if __name__ == '__main__':
    unittest.main()
