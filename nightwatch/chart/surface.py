"""Coalesce input before scene dispatch and present through one frame owner."""
import time
from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg
from ..presentation import performance_profile_active, record_frame_request, record_performance_count, record_performance_timing


class ChartGraphicsView(pg.GraphicsLayoutWidget):
    frame_needed = QtCore.Signal()
    frame_presented = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        self._paint_pending = False
        self._painting = False
        self._scene_dirty = False
        self._pending_pointer = None
        self._pointer_received_at = 0.0
        self._pointer_paint_sample_at = 0.0
        self.presentation_clock = None
        super().__init__(*args, **kwargs)


        self.setViewportUpdateMode(QtWidgets.QGraphicsView.ViewportUpdateMode.NoViewportUpdate)
        self.scene().changed.connect(self._scene_changed)

    def mouseMoveEvent(self, event):




        record_performance_count("input.pointer_received")
        already_pending = self._pending_pointer is not None
        if already_pending:
            record_performance_count("input.pointer_coalesced")
        self._pending_pointer = QtGui.QMouseEvent(event)
        self._pointer_received_at = time.monotonic()
        event.accept()
        if not already_pending:
            record_frame_request(self.viewport(), self._pointer_received_at)
            self.frame_needed.emit()

    def flush_pointer_motion(self):
        event, self._pending_pointer = self._pending_pointer, None
        if event is None:
            return
        started = time.perf_counter() if performance_profile_active() else 0.0
        if started:
            record_performance_timing("input.pointer_queue_ms", (time.monotonic() - self._pointer_received_at) * 1000)
        self._pointer_paint_sample_at = self._pointer_received_at
        super().mouseMoveEvent(event)
        record_performance_count("input.pointer_dispatched")
        if started:
            record_performance_timing("input.scene_dispatch_ms", (time.perf_counter() - started) * 1000)

    def mousePressEvent(self, event):
        self.flush_pointer_motion()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):


        self.flush_pointer_motion()
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        self.flush_pointer_motion()
        super().leaveEvent(event)

    def hideEvent(self, event):
        self._pending_pointer = None
        self._paint_pending = False
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self.request_redraw()

    def _scene_changed(self, _rects):
        record_performance_count("chart.scene_changed_signals")
        self.request_redraw()

    def request_redraw(self):
        record_performance_count("chart.redraw_requests")
        self._scene_dirty = True
        if not self._paint_pending and not self._painting:
            record_performance_count("chart.redraw_frame_needed_emits")
            self.frame_needed.emit()
        else:
            record_performance_count("chart.redraw_coalesced")

    def present(self):
        record_performance_count("chart.present_calls")
        viewport = self.viewport()
        if self._painting or not self.isVisible() or not viewport.isVisible() or self.window().isMinimized():
            record_performance_count("chart.present_blocked_visibility_or_paint")
            return False
        clock = self.presentation_clock
        if clock is not None and clock.frame_pending():
            record_performance_count("chart.present_blocked_frame_pending")
            return False


        self._paint_pending = True
        record_frame_request(viewport)
        if clock is not None:
            clock.frame_submitted(viewport)
        viewport.update()
        record_performance_count("chart.present_submissions")
        return True

    def paintEvent(self, event):
        record_performance_count("chart.graphics_paint_events")
        self._paint_pending = False
        self._painting = True
        clock = self.presentation_clock
        viewport = self.viewport()
        pointer_received_at, self._pointer_paint_sample_at = self._pointer_paint_sample_at, 0.0
        started = 0.0
        try:


            if clock is not None:
                clock.begin_frame(viewport, input_received_at=pointer_received_at)
            self._scene_dirty = False
            started = time.perf_counter() if performance_profile_active() else 0.0


            self.scene().prepareForPaint()
            prepared = time.perf_counter() if started else 0.0
            QtWidgets.QGraphicsView.paintEvent(self, event)
            if started:
                record_performance_timing("qt.scene_prepare_ms", (prepared - started) * 1000)
                record_performance_timing("qt.scene_draw_ms", (time.perf_counter() - prepared) * 1000)
        finally:
            if started:
                record_performance_timing("qt.chart_paint_ms", (time.perf_counter() - started) * 1000)
                if pointer_received_at:
                    record_performance_timing("input.pointer_to_paint_ms", (time.monotonic() - pointer_received_at) * 1000)
            self._painting = False
            if clock is not None:
                clock.end_frame(viewport)
        self.frame_presented.emit()
        if self._scene_dirty:
            self.frame_needed.emit()
