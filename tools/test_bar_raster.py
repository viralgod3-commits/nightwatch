"""Offline pixel and memory checks for dense candle fallback rendering.

Run: QT_QPA_PLATFORM=offscreen python tools/test_bar_raster.py
The reference uses Qt's winding path and the original eager pan margins.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from nightwatch.chart.rendering import PixelBarBatch
from nightwatch.theme import CANDLE_STYLES


class PathReference(PixelBarBatch):
    def _compact_cpu_geometry(self, key, style):
        return self.interactive_resize

    def _paint_overlap_layer(self, *args):
        return False  # Compare against the winding-fill fallback.


class BarRasterChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_candles_and_volume_match_through_camera_changes(self):
        x = np.arange(2400, dtype=float)
        opened = 100 + np.sin(x * .03) * 8
        closed = opened + np.sin(x * .37) * 1.1
        matrix = np.column_stack((x, opened, np.maximum(opened, closed) + .8,
            np.minimum(opened, closed) - .8, closed, 200 + 100 * np.cos(x * .1), 1000 + x))
        frames = ((.8, 600), (.8, 600), (.8, 601.6), (.8, 660),
                  (.8, 960), (1.1, 600), (1.1, 602.8), (1.1, 960), (1.7, 650))
        for dpr in (1., 1.5, 2.):
            for logarithmic in (False, True):
                for opacity in (1., .6):
                    for name, style in CANDLE_STYLES.items():
                        for volume in (False, True):
                            batches = (PathReference(), PixelBarBatch())
                            for batch in batches:
                                batch.set_data(matrix, 1, logarithmic=logarithmic, volume=volume)
                            for frame, (scale, offset) in enumerate(frames):
                                images = []
                                for batch in batches:
                                    image = QtGui.QImage(int(800*dpr), int(560*dpr),
                                        QtGui.QImage.Format.Format_ARGB32_Premultiplied)
                                    image.setDevicePixelRatio(dpr)
                                    image.fill(QtGui.QColor('#121314'))
                                    painter = QtGui.QPainter(image)
                                    try:
                                        painter.setClipRect(QtCore.QRectF(20, 20, 740, 510))
                                        painter.setOpacity(opacity)
                                        transform = QtGui.QTransform()
                                        transform.translate(20, 275)
                                        transform.scale(scale, -(11000 if logarithmic else 23))
                                        transform.translate(-offset, -(np.log10(100) if logarithmic else 100))
                                        painter.setTransform(transform)
                                        batch.paint(painter, style, '#121314', '#39d98a', '#ff5566',
                                            volume=volume, volume_overlay_max=300 if volume else 0,
                                            volume_overlay_fraction=.19 if volume else 0,
                                            volume_overlay_screen=QtCore.QRectF(20*dpr, 20*dpr, 740*dpr, 510*dpr)
                                                if volume else None, force_cpu=True)
                                    finally:
                                        painter.end()
                                    images.append(bytes(image.constBits()))
                                with self.subTest(dpr=dpr, logarithmic=logarithmic, opacity=opacity,
                                                  style=name, volume=volume, frame=frame):
                                    self.assertTrue(images[0] == images[1], 'Rendered pixels changed')

    def test_overlapping_coverage_and_tile_seams_match(self):
        # Cross the 16 MiB surface boundary with fractional rectangle edges.
        size = QtCore.QSize(2000, 2200)
        rects = [QtCore.QRectF((i*17) % 1900 + .25, (i*41) % 2110 + .5, 80.5, 110.25)
                 for i in range(1024)]
        for alpha in (1, 34, 128, 254):
            images = []
            batches = (PathReference(), PixelBarBatch())
            for batch in batches:
                color = QtGui.QColor('#39d98a')
                color.setAlpha(alpha)
                batch.batches = [(QtGui.QBrush(color), rects)]
                image = QtGui.QImage(size, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
                image.fill(QtGui.QColor('#121314'))
                painter = QtGui.QPainter(image)
                try:
                    painter.setPen(QtCore.Qt.PenStyle.NoPen)
                    batch._draw_cpu_batches(painter, QtCore.QRectF(0, 0, size.width(), size.height()))
                finally:
                    painter.end()
                images.append(bytes(image.constBits()))
            with self.subTest(alpha=alpha):
                self.assertTrue(images[0] == images[1], 'Coverage or tile seam changed')
                self.assertGreater(batches[1]._cpu_overlap_layer.sizeInBytes(), 0)
                self.assertLessEqual(batches[1]._cpu_overlap_layer.sizeInBytes(), 16*1024*1024)

    def test_live_and_nonstandard_blending_use_the_small_path(self):
        for count, opacity, mode in ((8, 1., QtGui.QPainter.CompositionMode_SourceOver),
                                     (600, .6, QtGui.QPainter.CompositionMode_SourceOver),
                                     (600, 1., QtGui.QPainter.CompositionMode_Source)):
            images = []
            batches = (PathReference(), PixelBarBatch())
            for batch in batches:
                batch.batches = [(QtGui.QBrush(QtGui.QColor(57, 217, 138, 34)),
                    [QtCore.QRectF(i % 20, i % 30, 45, 50) for i in range(count)])]
                image = QtGui.QImage(100, 100, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
                image.fill(QtGui.QColor('#121314'))
                painter = QtGui.QPainter(image)
                try:
                    painter.setPen(QtCore.Qt.PenStyle.NoPen)
                    painter.setOpacity(opacity)
                    painter.setCompositionMode(mode)
                    batch._draw_cpu_batches(painter, QtCore.QRectF(0, 0, 100, 100))
                finally:
                    painter.end()
                images.append(bytes(image.constBits()))
            self.assertTrue(images[0] == images[1], 'Fallback pixels changed')
            self.assertTrue(batches[1]._cpu_overlap_layer.isNull())


if __name__ == '__main__':
    unittest.main()
