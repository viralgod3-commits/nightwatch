"""Bounded damage regions and pixel-exact copies for the leased DOM images."""
from __future__ import annotations

import math

from PySide6 import QtCore, QtGui


MAX_REGION_RECTS = 64
FULL_REGION_AREA_RATIO = .75


def region_area(region: QtGui.QRegion) -> int:
    return sum(rect.width() * rect.height() for rect in region)


def bounded_region(region: QtGui.QRegion, bounds: QtCore.QRect) -> QtGui.QRegion:
    """Keep cumulative damage independent of the number of skipped frames."""
    clipped = region.intersected(bounds)
    if clipped.rectCount() > MAX_REGION_RECTS:
        clipped = QtGui.QRegion(clipped.boundingRect())
    if region_area(clipped) >= bounds.width() * bounds.height() * FULL_REGION_AREA_RATIO:
        return QtGui.QRegion(bounds)
    return clipped


def pixel_region(region: QtGui.QRegion, dpr: float, bounds: QtCore.QRect,
                 *, inward: bool = False) -> QtGui.QRegion:
    """Round damage outwards, or round guaranteed repaint coverage inwards.

    Fractional-DPI edge pixels must be synchronized unless the next paint is
    guaranteed to replace them. An outward region is unsafe to subtract from
    buffer debt because it can include pixels outside the actual paint clip.
    """
    start, end = (math.ceil, math.floor) if inward else (math.floor, math.ceil)
    pixels = QtGui.QRegion()
    for rect in region:
        left, top = start(rect.x() * dpr), start(rect.y() * dpr)
        right, bottom = end((rect.x() + rect.width()) * dpr), end((rect.y() + rect.height()) * dpr)
        if right > left and bottom > top:
            pixels += QtCore.QRect(left, top, right - left, bottom - top)
    return pixels.intersected(bounds)


def copy_pixel_region(target: QtGui.QImage, source: QtGui.QImage,
                      region: QtGui.QRegion) -> None:
    """Copy only physical pixels, with fresh zero-copy, DPR=1 image wrappers."""
    if region.isEmpty():
        return
    # Independent wrappers avoid DPR transforms, implicit QImage detachment and
    # paint-engine cache keys that do not notice writes through another wrapper.
    raw_target = QtGui.QImage(target.bits(), target.width(), target.height(),
                             target.bytesPerLine(), target.format())
    raw_source = QtGui.QImage(source.constBits(), source.width(), source.height(),
                             source.bytesPerLine(), source.format())
    painter = QtGui.QPainter(raw_target)
    try:
        painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_Source)
        painter.setClipRegion(region)
        painter.drawImage(QtCore.QPoint(), raw_source)
    finally:
        painter.end()


def draw_native_region(painter: QtGui.QPainter, image: QtGui.QImage,
                       region: QtGui.QRegion) -> int:
    """Blit native pixels through the painter's already-installed region clip.

    Qt's raster engine copies the clipped spans directly. A point draw avoids
    source/destination resampling and a Python draw call for every rectangle.
    """
    painter.drawImage(QtCore.QPointF(), image)
    dpr = image.devicePixelRatioF()
    if region.rectCount() == 1:
        rect = region.boundingRect()
        if (rect.x() == rect.y() == 0
                and math.ceil(rect.width() * dpr) == image.width()
                and math.ceil(rect.height() * dpr) == image.height()):
            return image.width() * image.height()
    pixels = pixel_region(region, dpr, image.rect())
    return region_area(pixels)
