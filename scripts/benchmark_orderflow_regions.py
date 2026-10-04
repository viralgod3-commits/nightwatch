"""Offline leased-buffer and native-blit profiling with pixel parity checks.

Synthetic damage isolates synchronization/blit costs from ladder preparation.
NIGHTWATCH_BENCH_SOURCE selects a baseline checkout for the same harness.
No exchange, gateway or live order is created; this is not a display-FPS claim.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import time

SOURCE = Path(os.environ.get('NIGHTWATCH_BENCH_SOURCE', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(SOURCE))
if os.environ.get('NIGHTWATCH_QT_SITE_PACKAGES'):
    sys.path.append(os.environ['NIGHTWATCH_QT_SITE_PACKAGES'])
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from PySide6 import QtCore, QtGui, QtWidgets
from nightwatch.orderbook.orderbook_ui import _DomRasterProcess, _map_dom_frame

try:
    from nightwatch.orderbook.raster_regions import bounded_region, draw_native_region
except ModuleNotFoundError as error:
    if error.name != 'nightwatch.orderbook.raster_regions':
        raise
    bounded_region = draw_native_region = None

def summary(values):
    ordered = sorted(values)
    return dict(median_ms=round(statistics.median(values), 5),
                p95_ms=round(ordered[round((len(values) - 1) * .95)], 5),
                p99_ms=round(ordered[round((len(values) - 1) * .99)], 5))


def image_like(image, background):
    result = QtGui.QImage(image.size(), image.format())
    result.setDevicePixelRatio(image.devicePixelRatioF())
    result.setDotsPerMeterX(image.dotsPerMeterX())
    result.setDotsPerMeterY(image.dotsPerMeterY())
    result.fill(background)
    return result


def pixels(image):
    return bytes(image.constBits())


def pattern(image, region, iteration):
    painter = QtGui.QPainter(image)
    try:
        painter.setClipRegion(region)
        painter.fillRect(region.boundingRect(), QtGui.QColor(4, 8, 12))
        painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
        # Full-width stripes, text and alpha expose stale rows and DPI-edge
        # resampling; the retained reference uses exactly the same paint clip.
        for rect in region:
            for y in range(rect.top() - 5, rect.bottom() + 12, 11):
                color = QtGui.QColor((iteration * 17 + y) % 256,
                                    (iteration * 31 + y * 3) % 256,
                                    (iteration * 7 + y * 5) % 256, 190)
                painter.fillRect(QtCore.QRect(0, y, image.width(), 4), color)
                painter.setPen(QtGui.QColor(220, 225, 230))
                painter.drawText(QtCore.QPointF(13, y + 10), f'{iteration:06d}  12345.67  Bid Ask')
    finally:
        painter.end()


def damage(case, iteration, bounds):
    width, height = bounds.width(), bounds.height()
    if case == 'full_churn':
        return QtGui.QRegion(bounds)
    if case == 'bbo_bands':
        return (QtGui.QRegion(QtCore.QRect(4, 5, width - 8, 31))
                + QtCore.QRect(4, height // 2, width - 8, 29))
    if case == 'same_row':
        return QtGui.QRegion(QtCore.QRect(4, 251, width - 8, 21))
    if case == 'fragmented':
        region = QtGui.QRegion()
        for i in range(100):
            region += QtCore.QRect(3 + (i % 10) * 71, 53 + (i // 10) * 71, 19, 9)
        return region
    if case == 'disjoint_rows':
        region = QtGui.QRegion()
        for i in range(6):
            region += QtCore.QRect(4, 103 + i * 103 + iteration % 13, width - 8, 17)
        return region
    return QtGui.QRegion(QtCore.QRect(4, 53 + iteration * 31 % (height - 100), width - 8, 21))


def measure(app, case, dpr, samples):
    worker = _DomRasterProcess({'theme': {}, 'dpi': 96})
    canvas = worker.canvas
    canvas.resize(803, 907)
    canvas._raster_dpr = dpr
    worker.epoch = worker.market_epoch = 0
    app.processEvents()
    for timer in canvas.findChildren(QtCore.QTimer):
        timer.stop()
    lease = None
    result = None
    reference = view = None
    times = dict(buffer_sync=[], gui_blit=[])
    copied_bytes = submitted_pixels = full_copy_bytes = full_blit_pixels = 0
    frames = adopted = held_frames = 0
    region_counts = []
    same_pixels = immutable_lease = view_matches = True
    no_paint_republished = True
    try:
        for iteration in range(samples + 2):
            # QImages must be released before their mapping owner, including
            # when resize creates another shared allocation.
            mapped_image = held_image = image = None
            if case == 'resize_reset' and iteration and iteration % 7 == 0:
                canvas.resize(803 + iteration % 3, 907 + iteration % 5)
                canvas._raster_dpr = 1.25 if canvas._raster_dpr == dpr else dpr
                canvas.reset()
                worker.market_epoch += 1
                app.processEvents()
                for timer in canvas.findChildren(QtCore.QTimer):
                    timer.stop()
                lease = result = previous_result = None
                reference = view = None
            dirty = QtGui.QRegion(canvas.rect()) if reference is None else damage(case, iteration, canvas.rect())
            if bounded_region and lease is not None and iteration % 7 == 0:
                if case == 'legacy_lease':
                    lease = lease[:2]
                elif case == 'stale_revision':
                    lease = (*lease[:2], lease[2] - 1)
            canvas._dirty_pixels = dirty
            held_image = worker.images[lease[1]] if lease is not None and lease[0] == worker.memory.name else None
            held_pixels = pixels(held_image) if held_image is not None else None
            previous_slot, previous_memory = worker.previous_slot, worker.memory
            started = time.perf_counter()
            slot, image = worker._surface(lease)
            dirty = canvas._dirty_pixels
            if bounded_region:
                dirty = bounded_region(dirty, canvas.rect())
                worker._synchronize_surface(slot, dirty)
            sync_ms = (time.perf_counter() - started) * 1000
            canvas._dirty_pixels = QtGui.QRegion()
            changing_slot = previous_slot is not None and previous_slot != slot and previous_memory is worker.memory
            copy_size = image.width() * image.height() * 4 if changing_slot else 0
            if iteration > 1:
                times['buffer_sync'].append(sync_ms)
                full_copy_bytes += copy_size
                copied_bytes += worker._sync_stats['raster_sync_last_bytes'] if bounded_region else copy_size
            if reference is None or reference.size() != image.size() or reference.devicePixelRatioF() != image.devicePixelRatioF():
                reference, view = image_like(image, canvas._bg), image_like(image, canvas._bg)
            pattern(image, dirty, iteration)
            pattern(reference, dirty, iteration)
            if bounded_region:
                worker._surface_painted(slot, dirty)
            same_pixels &= pixels(image) == pixels(reference)
            if held_image is not None:
                immutable_lease &= pixels(held_image) == held_pixels
            frames += 1
            # Multiple unadopted renders must accumulate relative to one held
            # lease, without changing that slot. This covers a discarded reply.
            if case == 'held_lease' and iteration % 6 != 0 and iteration != samples + 1:
                held_frames += 1
                continue
            previous_result = result
            result = dict(frame=None, epoch=worker.epoch, market_epoch=worker.market_epoch)
            if bounded_region:
                worker._publish_surface(result, lease)
                if case == 'unpublished_reply':
                    revision = result['frame_revision']
                    result = worker.step([], lease)
                    no_paint_republished &= result.get('frame_revision') == revision and result.get('frame') is not None
            else:
                result['frame'] = (worker.memory.name, slot, *worker.shape[:3])
            result = _map_dom_frame(result)
            if bounded_region and previous_result is not None and result.get('frame_base') == lease:
                repaint = QtGui.QRegion()
                for rect in result['dirty_rects']:
                    repaint += QtCore.QRect(*rect)
            else:
                repaint = QtGui.QRegion(canvas.rect())
            region_counts.append(repaint.rectCount())
            mapped_image = result['pixels'].images[slot]
            painter = QtGui.QPainter(view)
            painter.setClipRegion(repaint)
            started = time.perf_counter()
            if draw_native_region:
                submitted = draw_native_region(painter, mapped_image, repaint)
            else:
                painter.drawImage(QtCore.QPointF(), mapped_image)
                submitted = image.width() * image.height()
            painter.end()
            elapsed = (time.perf_counter() - started) * 1000
            if iteration > 1:
                times['gui_blit'].append(elapsed)
                submitted_pixels += submitted
                full_blit_pixels += image.width() * image.height()
            # A full native draw is the GUI pixel reference, including ceil-DPI
            # clipping at the panel boundary; no throughput timing includes it.
            expected = image_like(image, canvas._bg)
            expected_painter = QtGui.QPainter(expected)
            expected_painter.setClipRegion(QtGui.QRegion(canvas.rect()))
            expected_painter.drawImage(QtCore.QPointF(), reference)
            expected_painter.end()
            view_matches &= pixels(view) == pixels(expected)
            adopted += 1
            lease = (*result['frame'][:2], result['frame_revision']) if bounded_region else result['frame'][:2]
        return dict(case=case, dpr=dpr, frames=frames, adopted_frames=adopted,
                    held_frames=held_frames, complete_buffer_pixels_preserved=same_pixels,
                    leased_pixels_immutable=immutable_lease,
                    gui_pixels_match_full_native_blit=view_matches,
                    no_paint_reply_republished=no_paint_republished if bounded_region else None,
                    max_region_rects=max(region_counts),
                    copied_bytes=copied_bytes, baseline_copy_bytes=full_copy_bytes,
                    submitted_blit_pixels=submitted_pixels, baseline_blit_pixels=full_blit_pixels,
                    timings={name: summary(data) for name, data in times.items()})
    finally:
        # Drop wrappers before the worker closes its shared-memory handles.
        mapped_image = held_image = image = None
        result = previous_result = None
        worker.close()
        app.processEvents()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=48)
    parser.add_argument('--dpr', type=float, nargs='+', default=[1, 1.1, 1.25, 1.5, 1.75, 2])
    parser.add_argument('--cases', nargs='+', default=[
        'bbo_bands', 'same_row', 'moving_row', 'disjoint_rows', 'held_lease',
        'unpublished_reply', 'full_churn', 'fragmented', 'resize_reset'])
    parser.add_argument('--label', default='working-tree')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.samples < 8 or any(not math.isfinite(dpr) or dpr <= 0 for dpr in args.dpr):
        parser.error('Use at least eight samples and finite positive DPR values')
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    report = dict(label=args.label, python=platform.python_version(), qt=QtCore.qVersion(),
                  platform=platform.platform(), regional_raster=bounded_region is not None,
                  measurement='synthetic damage; actual shared buffers and native blits; excludes ladder preparation, pipes and display FPS',
                  cases=[measure(app, case, dpr, args.samples) for dpr in args.dpr for case in args.cases])
    output = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.write_text(output)
    print(output, end='')


if __name__ == '__main__':
    main()
