"""Offline calendar and rendered-axis regressions for retained panning layouts.

Run with QT_QPA_PLATFORM=offscreen python tools/test_axis_panning.py.
Repeat with TZ=America/New_York / Asia/Kolkata and QT_SCALE_FACTOR=1.5 / 2
for daylight-saving, fractional-offset and per-monitor scaling coverage.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

from nightwatch.chart.axis import CalendarTickCache
from nightwatch.chart.workspace import TimeAxisItem, _chart_axis_font
from nightwatch.utilities import load_app_fonts


class UncachedCalendar:
    def tick_values(self, level, minimum, maximum, spacing):
        return level.tickValues(minimum, maximum, minSpc=spacing)


class ReferenceTimeAxis(TimeAxisItem):
    def __init__(self):
        super().__init__('bottom')
        self._calendar_ticks = UncachedCalendar()

    def generateDrawSpecs(self, painter):
        return pg.DateAxisItem.generateDrawSpecs(self, painter)


class AxisPanningChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        load_app_fonts(cls.app, str(ROOT))

    def test_calendar_matches_upstream_at_boundaries(self):
        axis = TimeAxisItem('bottom')
        cache = CalendarTickCache()
        # Leap day, spring/fall DST, year boundary, and pre-epoch dates.
        anchors = (1709164800., 1710054000., 1730613600., 1735689600., -86400.)
        hits = 0
        for level in axis.zoomLevels.values():
            unit = level.tickSpecs[-1].spacing
            for offset in (None, 0, -19800, 3600):
                level.utcOffset = offset
                for anchor in anchors:
                    for spacing in (unit * .8, unit * 1.01, unit * 6.01):
                        for delta in (0, .00001, -.00001, .1, .25, 1, 1.00001, 3, -.1):
                            minimum = anchor + delta * unit
                            maximum = minimum + unit * 21
                            previous = cache.values
                            actual = cache.tick_values(level, minimum, maximum, spacing)
                            expected = level.tickValues(minimum, maximum, minSpc=spacing)
                            with self.subTest(unit=unit, offset=offset, anchor=anchor, delta=delta):
                                self.assertEqual(actual, expected)
                            hits += actual is previous
        self.assertGreater(hits, 100)

    def test_rendering_matches_uncached_layout_during_pan_and_invalidation(self):
        widgets, axes = [], []
        for axis in (TimeAxisItem('bottom'), ReferenceTimeAxis()):
            widget = pg.GraphicsLayoutWidget()
            widget.addPlot(axisItems={'bottom': axis})
            axis.setTickFont(_chart_axis_font())
            widget.resize(920, 400)
            widget.show()
            widgets.append(widget)
            axes.append(axis)
        self.app.processEvents()
        option = QtWidgets.QStyleOptionGraphicsItem()
        hits = 0

        def render(axis, minimum, span):
            axis.setRange(minimum, minimum + span)
            bounds = axis.boundingRect().toAlignedRect()
            image = QtGui.QImage(bounds.size() * self.app.devicePixelRatio(),
                                 QtGui.QImage.Format.Format_ARGB32_Premultiplied)
            image.setDevicePixelRatio(self.app.devicePixelRatio())
            image.fill(QtGui.QColor('#101923'))
            painter = QtGui.QPainter(image)
            try:
                painter.translate(-bounds.left(), -bounds.top())
                axis.paint(painter, option)
            finally:
                painter.end()
            return image.size(), bytes(image.constBits())

        def compare(minimum, span):
            nonlocal hits
            previous = getattr(axes[0], '_pan_layout', None)
            self.assertEqual(render(axes[0], minimum, span), render(axes[1], minimum, span))
            hits += previous is not None and previous is axes[0]._pan_layout

        try:
            for span in (.6, 14., 600., 14400., 86400., 604800., 7776000., 43200000., 630720000.):
                for direction in (1, -1):
                    for frame in range(60):
                        with self.subTest(span=span, direction=direction, frame=frame):
                            compare(1729900800. + direction * frame * span / 600, span)
            # Exercise invalidation without leaving the cached tick interval.
            for change in range(24):
                for widget, axis in zip(widgets, axes):
                    axis.utcOffset = (0, 3600, -19800, None)[change % 4]
                    axis.picture = None
                    axis.setStyle(tickTextOffset=change % 7,
                                  hideOverlappingLabels=bool(change % 2),
                                  showValues=change % 5 != 0)
                    axis.setGrid(90 if change % 6 == 0 else False)
                    font = _chart_axis_font()
                    font.setPointSizeF(font.pointSizeF() + change % 3)
                    axis.setTickFont(font)
                    axis.setPen(pg.mkPen('#527789' if change % 2 else '#f2dabb'))
                    axis.setTextPen(pg.mkPen('#dae1ee' if change % 2 else '#223142'))
                    widget.resize(620 if change % 2 else 1230, 400)
                self.app.processEvents()
                for frame in range(12):
                    with self.subTest(change=change, frame=frame):
                        compare(1729900800. + frame * 6, 3600.)
            # UTC offset and the in-place tickTextOffset setter can change a
            # label while the selected ticks and camera dimensions stay fixed.
            for axis in axes:
                axis.setGrid(False)
                axis.setStyle(showValues=True)
                axis.utcOffset = 0
                axis.picture = None
            compare(1729900800., 14.)
            for axis in axes:
                axis.utcOffset = 3600
                axis.picture = None
                axis.setStyle(tickTextOffset=11)
            compare(1729900800.01, 14.)
            self.assertGreater(hits, 100)
        finally:
            for widget in widgets:
                widget.close()
                widget.deleteLater()
            self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
