"""Completion-driven chart presentation and coalesced secondary UI transactions.

Charts prepare the next frame outside painting when composition completes.
One queued event coalesces producer state before the Qt paint. Timers recover
missing completions or service surfaces without swap feedback. Secondary UI
clocks remain dirty-driven.
"""
from __future__ import annotations

import math
import time
import gc
from functools import wraps
from collections import deque
from collections.abc import Callable
from typing import Any
from PySide6 import QtCore, QtWidgets
from PySide6.QtCore import Qt, Signal

_PREPARE_FRAME_EVENT = QtCore.QEvent.Type(QtCore.QEvent.registerEventType())


_PROFILE_DIAGNOSTIC_STARTED_AT = 0.0
_PROFILE_DIAGNOSTIC_DURATION_S = 0.0
_PROFILE_DIAGNOSTIC_TIMINGS: dict[str, list[float]] = {}
_PROFILE_DIAGNOSTIC_COUNTS: dict[str, int] = {}
_PROFILE_DIAGNOSTIC_SUMS: dict[str, float] = {}
_PROFILE_PENDING_PAINT_REQUESTS: dict[int, float] = {}
_PROFILE_REQUESTED_PAINT_TIMESTAMPS: dict[int, list[float]] = {}
_PROFILE_PAINT_REQUEST_LATENCIES: dict[int, list[float]] = {}


def performance_profile_active() -> bool:
    global _PROFILE_DIAGNOSTIC_STARTED_AT, _PROFILE_DIAGNOSTIC_DURATION_S
    started = _PROFILE_DIAGNOSTIC_STARTED_AT
    duration = _PROFILE_DIAGNOSTIC_DURATION_S
    if started <= 0.0 or duration <= 0.0:
        return False
    if time.monotonic() <= started + duration:
        return True

    _PROFILE_DIAGNOSTIC_STARTED_AT = 0.0
    _PROFILE_DIAGNOSTIC_DURATION_S = 0.0
    return False


def record_performance_timing(name: str, duration_ms: float) -> None:
    if not performance_profile_active():
        return
    _PROFILE_DIAGNOSTIC_TIMINGS.setdefault(str(name), []).append(max(0.0, float(duration_ms)))


def record_performance_count(name: str, amount: int = 1) -> None:
    if not performance_profile_active():
        return
    key = str(name)
    _PROFILE_DIAGNOSTIC_COUNTS[key] = _PROFILE_DIAGNOSTIC_COUNTS.get(key, 0) + int(amount)


def record_performance_sum(name: str, value: float) -> None:
    if not performance_profile_active():
        return
    key = str(name)
    _PROFILE_DIAGNOSTIC_SUMS[key] = _PROFILE_DIAGNOSTIC_SUMS.get(key, 0.0) + float(value)


def profile_callback(name: str):
    """Time a complete application callback only during F3 capture."""
    def decorate(function):
        @wraps(function)
        def measured(*args, **kwargs):
            if not performance_profile_active():
                return function(*args, **kwargs)
            started = time.perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                record_performance_timing(name, (time.perf_counter() - started) * 1000)
        return measured
    return decorate


def _reset_performance_diagnostics(started_at: float, duration_s: float) -> None:
    global _PROFILE_DIAGNOSTIC_STARTED_AT, _PROFILE_DIAGNOSTIC_DURATION_S
    _PROFILE_DIAGNOSTIC_STARTED_AT = float(started_at)
    _PROFILE_DIAGNOSTIC_DURATION_S = float(duration_s)
    _PROFILE_DIAGNOSTIC_TIMINGS.clear()
    _PROFILE_DIAGNOSTIC_COUNTS.clear()
    _PROFILE_DIAGNOSTIC_SUMS.clear()
    _PROFILE_PENDING_PAINT_REQUESTS.clear()
    _PROFILE_REQUESTED_PAINT_TIMESTAMPS.clear()
    _PROFILE_PAINT_REQUEST_LATENCIES.clear()


def record_frame_request(source: object, timestamp: float | None = None) -> None:
    """Mark the first outstanding request that should result in a viewport paint.

    Repeated dirties inside the same event-loop turn are intentionally coalesced: the
    first request timestamp is retained until the next paint so the profiler measures
    user-visible request -> paint latency instead of counting producer spam as frames.
    """
    if source is None or not performance_profile_active():
        return
    key = id(source)
    stamp = time.monotonic() if timestamp is None else float(timestamp)
    if key in _PROFILE_PENDING_PAINT_REQUESTS:
        record_performance_count("qt.paint_requests_coalesced")
        return
    _PROFILE_PENDING_PAINT_REQUESTS[key] = stamp
    record_performance_count("qt.paint_requests")

def _performance_diagnostic_summary() -> dict[str, object]:
    timings: dict[str, dict[str, float | int]] = {}
    for name, samples in list(_PROFILE_DIAGNOSTIC_TIMINGS.items()):
        values = list(samples)
        if not values:
            continue
        ordered = sorted(values)
        def percentile(fraction: float) -> float:
            index = int(round((len(ordered) - 1) * fraction))
            return ordered[max(0, min(len(ordered) - 1, index))]
        timings[name] = {
            "count": len(values),
            "total_ms": sum(values),
            "avg_ms": sum(values) / len(values),
            "p95_ms": percentile(0.95),
            "p99_ms": percentile(0.99),
            "max_ms": max(values),
        }
    return {
        "timings": timings,
        "counts": dict(_PROFILE_DIAGNOSTIC_COUNTS),
        "sums": dict(_PROFILE_DIAGNOSTIC_SUMS),
    }


def display_refresh_rate(widget: QtWidgets.QWidget | None = None) -> float:
    """Return the active screen refresh rate without imposing an artificial cap."""
    screen = widget.screen() if widget is not None else None
    if screen is None:
        application = QtWidgets.QApplication.instance()
        screen = application.primaryScreen() if application is not None else None
    refresh = float(screen.refreshRate()) if screen is not None else 60.0
    if not math.isfinite(refresh) or refresh <= 0.0:
        refresh = 60.0
    return refresh


def display_frame_interval_ms(widget: QtWidgets.QWidget | None = None) -> int:
    """Best QTimer approximation of one active-display period.

    QTimer is millisecond-granularity, so 1 ms is the only implementation floor;
    there is no product FPS ceiling such as 60/240/360 Hz.
    """
    return max(1, int(round(1000.0 / display_refresh_rate(widget))))


class PresentationClock(QtCore.QObject):
    """One coalescing presentation clock for a top-level Nightwatch window."""

    interaction_frame = Signal(float)
    frame = Signal(float)

    def __init__(
        self,
        owner: QtWidgets.QWidget | None = None,
        parent: QtCore.QObject | None = None,
    ) -> None:
        super().__init__(parent or owner)
        self._owner = owner
        self._requested = False
        self._flushing = False
        self._frame_timestamps: deque[float] = deque()
        self._next_frame_deadline = 0.0
        self._pacing_source = None
        self._pacing_window = None
        self._frame_presenter: Callable[[], bool] | None = None
        self._prepare_queued = False
        self._prepare_queued_at = 0.0
        self._frame_epoch = 0
        self._paint_input_received_at = 0.0
        self._continuous = False
        self._final_frame_requested = False
        self._awaiting_paint = False
        self._painting = False
        self._awaiting_swap = False
        self._submitted_at = 0.0
        self._paint_started_at = 0.0
        self._paint_completed_at = 0.0
        self._compose_started_at = 0.0
        self._paint_timestamps: dict[int, deque[float]] = {}
        self._profile_started_at = 0.0
        self._profile_duration_s = 0.0
        self._profile_frame_timestamps: list[float] = []
        self._profile_paint_timestamps: dict[int, list[float]] = {}
        self._profile_probe_intervals_ms: list[float] = []
        self._profile_probe_last = 0.0
        self._profile_dispatcher = None
        self._profile_awake_at = 0.0
        self._profile_gc_started: dict[int, float] = {}
        self._frame_sources: dict[int, QtWidgets.QWidget] = {}
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._flush)


        self._profile_probe_timer = QtCore.QTimer(self)
        self._profile_probe_timer.setSingleShot(False)
        self._profile_probe_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._profile_probe_timer.timeout.connect(self._profile_probe_tick)

    def interval_ms(self) -> int:
        return display_frame_interval_ms(self._owner)

    def set_frame_presenter(self, presenter: Callable[[], bool]) -> None:
        """Bind a chart's asynchronous viewport-update boundary, once."""
        self._frame_presenter = presenter

    def set_continuous(self, active: bool) -> None:
        """Run completion-paced commits only for an active visual transition."""
        active = bool(active)
        if active == self._continuous:
            return
        self._continuous = active
        if active:
            self._final_frame_requested = False
            self._schedule_frame(immediate=True)
        else:


            self._final_frame_requested = True
            self.request(immediate=True)

    def presentation_required(self) -> bool:
        """Active transitions and their final frame bypass the idle dirty gate."""
        return self._continuous or self._final_frame_requested

    def frame_pending(self) -> bool:
        """Whether a paint or its composition already owns the next completion."""
        return self._awaiting_paint or self._painting or self._awaiting_swap

    def event(self, event: QtCore.QEvent) -> bool:
        if event.type() != _PREPARE_FRAME_EVENT:
            return super().event(event)
        self._prepare_queued = False
        source = self._pacing_source
        if (source is None or not (self._requested or self._continuous)
                or not source.isVisible() or source.window().isMinimized()
                or self.frame_pending()):
            return True
        now = time.monotonic()
        epoch = self._frame_epoch
        record_performance_timing("qt.frame_prepare_queue_ms", (now - self._prepare_queued_at) * 1000)
        self._commit_frame(now)


        if epoch == self._frame_epoch and source is self._pacing_source:
            submitted = self._frame_presenter()
            if submitted:
                self._final_frame_requested = False
            elif not self.frame_pending():
                if self.presentation_required():
                    self._requested |= self._final_frame_requested


                    if source.isVisible() and not source.window().isMinimized():
                        self._timer.start(self.interval_ms())
                elif self._requested:

                    self._schedule_frame()
        elif epoch == self._frame_epoch and (self._requested or self._continuous):
            self._schedule_frame()
        return True

    def register_frame_source(self, widget: QtWidgets.QWidget | None, *, pace: bool = False) -> None:
        """Register a viewport whose real QPaintEvent cadence represents chart FPS."""
        if widget is None:
            return
        key = id(widget)
        if pace and self._pacing_source is not widget:
            self._pacing_source = widget
            self._awaiting_paint = False
            self._awaiting_swap = False
            self._timer.stop()
        if key not in self._frame_sources:
            self._frame_sources[key] = widget
            widget.installEventFilter(self)
            widget.destroyed.connect(lambda _obj=None, source_key=key: self._source_destroyed(source_key))
            if hasattr(widget, "frameSwapped"):
                widget.frameSwapped.connect(lambda source=widget: self._frame_swapped(source))
                widget.aboutToCompose.connect(lambda source=widget: self._about_to_compose(source))
        if pace:
            self._observe_pacing_window(widget)
            if self._continuous or self._requested:
                self._schedule_frame(immediate=True)

    def _source_destroyed(self, key: int) -> None:
        source = self._frame_sources.pop(key, None)
        self._paint_timestamps.pop(key, None)
        if source is self._pacing_source:
            self._pacing_source = None
            self._awaiting_paint = self._awaiting_swap = False
            try:
                self._timer.stop()
            except RuntimeError:


                pass

    def _observe_pacing_window(self, source: QtWidgets.QWidget) -> None:
        window = source.window()
        if window is self._pacing_window:
            return
        if self._pacing_window is not None:
            try:
                self._pacing_window.removeEventFilter(self)
            except RuntimeError:
                pass
        self._pacing_window = window
        window.installEventFilter(self)

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if id(watched) in self._frame_sources and event.type() == QtCore.QEvent.Type.Paint:
            self.record_chart_paint(watched)
        if watched is self._pacing_source or watched is self._pacing_window:
            if event.type() in (QtCore.QEvent.Type.Hide, QtCore.QEvent.Type.Destroy):
                self._awaiting_paint = self._awaiting_swap = False
                self._timer.stop()
            elif event.type() in (QtCore.QEvent.Type.Show, QtCore.QEvent.Type.WindowStateChange):
                if self._continuous or self._requested:
                    self._schedule_frame(immediate=True)
        return super().eventFilter(watched, event)

    def _uses_swap_feedback(self, source: QtWidgets.QWidget) -> bool:
        return bool(
            hasattr(source, "frameSwapped")
            and source.isValid()
            and source.format().swapInterval() != 0
        )

    def _completion_timeout(self) -> float:


        source = self._pacing_source
        interval = 1
        if source is not None and hasattr(source, "frameSwapped"):
            interval = max(1, source.format().swapInterval())
        return 2.0 * interval / display_refresh_rate(self._owner)

    def _arm_completion_watchdog(self) -> None:
        remaining = self._submitted_at + self._completion_timeout() - time.monotonic()
        self._timer.start(max(1, int(math.ceil(remaining * 1000.0))))

    def frame_submitted(self, source: QtWidgets.QWidget) -> None:
        """A real viewport update, not just a worker/label transaction."""
        if source is not self._pacing_source or not source.isVisible():
            return
        self._observe_pacing_window(source)
        self._awaiting_paint = True
        self._awaiting_swap = self._uses_swap_feedback(source)
        self._submitted_at = time.monotonic()
        self._paint_started_at = self._paint_completed_at = 0.0
        self._compose_started_at = 0.0
        self._arm_completion_watchdog()

    def begin_frame(self, source: QtWidgets.QWidget, *, input_received_at: float = 0.0) -> None:
        """Mark the draw boundary; all model/interaction work precedes this."""
        if source is not self._pacing_source or self._frame_presenter is None or self._painting:
            return
        now = time.monotonic()
        if self._awaiting_paint:
            record_performance_timing("qt.submit_to_paint_ms", (now - self._submitted_at) * 1000)
        else:
            self._submitted_at = now
        self._awaiting_paint = False
        self._painting = True
        self._paint_input_received_at = input_received_at
        self._paint_started_at = now
        self._paint_completed_at = self._compose_started_at = 0.0
        self._awaiting_swap = self._uses_swap_feedback(source)
        self._timer.stop()

    def end_frame(self, source: QtWidgets.QWidget) -> None:
        if source is not self._pacing_source or not self._painting:
            return
        self._painting = False
        self._paint_completed_at = time.monotonic()
        if self._awaiting_swap:
            self._arm_completion_watchdog()
        elif self._continuous or self._requested:


            self._timer.start(0)

    def _about_to_compose(self, source) -> None:
        if source is self._pacing_source and self._awaiting_swap and not self._awaiting_paint:
            self._compose_started_at = time.monotonic()

    def _frame_swapped(self, source) -> None:
        if source is not self._pacing_source or not self._awaiting_swap or self._awaiting_paint or self._painting:
            return
        now = time.monotonic()
        record_performance_timing("qt.submit_to_swap_ms", (now - self._submitted_at) * 1000)
        if self._paint_started_at:
            record_performance_timing("qt.paint_begin_to_swap_ms", (now - self._paint_started_at) * 1000)
        if self._paint_completed_at:
            record_performance_timing("qt.paint_end_to_swap_ms", (now - self._paint_completed_at) * 1000)
        if self._compose_started_at:
            record_performance_timing("qt.compose_swap_ms", (now - self._compose_started_at) * 1000)
        record_performance_count("qt.chart_swaps")
        if self._paint_input_received_at:
            record_performance_timing("input.pointer_to_swap_ms", (now - self._paint_input_received_at) * 1000)
            self._paint_input_received_at = 0.0
        self._awaiting_swap = False
        self._timer.stop()


        if self._continuous or self._requested:
            self._schedule_frame()

    def request(self, *, immediate: bool = False) -> None:


        self._requested = True
        self._schedule_frame(immediate=immediate)

    def _schedule_frame(self, *, immediate: bool = False) -> None:
        if self._flushing or self._painting:
            return
        if self._frame_presenter is not None:
            source = self._pacing_source
            if source is None or not source.isVisible() or source.window().isMinimized():
                self._awaiting_paint = self._awaiting_swap = False
                self._timer.stop()
                return
            if self._awaiting_paint or self._awaiting_swap:
                return
            if not self._prepare_queued:
                self._prepare_queued = True
                self._prepare_queued_at = time.monotonic()
                QtCore.QCoreApplication.postEvent(self, QtCore.QEvent(_PREPARE_FRAME_EVENT))
            return


        now = time.monotonic()
        remaining = max(0.0, self._next_frame_deadline - now)
        delay = 0 if immediate else max(0, int(round(remaining * 1000.0)))
        if self._timer.isActive():
            if delay < self._timer.remainingTime():
                self._timer.start(delay)
            return
        self._timer.start(delay)

    def cancel(self) -> None:
        self._frame_epoch += 1
        self._requested = False
        self._continuous = False
        self._final_frame_requested = False
        self._awaiting_paint = False
        self._painting = False
        self._awaiting_swap = False
        self._next_frame_deadline = 0.0
        self._timer.stop()
        self._prepare_queued = False
        QtCore.QCoreApplication.removePostedEvents(self, _PREPARE_FRAME_EVENT)

    def frame_rate(self, window_seconds: float = 1.0) -> tuple[float, float, bool]:
        """Return chart paint rate (or transaction rate without a chart source).

        Idle charts may paint zero times; secondary UI is also dirty-driven.
        Paints are not a claim of GPU scanout FPS.
        """
        now = time.monotonic()
        window = max(0.25, float(window_seconds))
        cutoff = now - window
        streams = list(self._paint_timestamps.values()) if self._frame_sources else [self._frame_timestamps]
        for frames in streams:
            while frames and frames[0] < cutoff:
                frames.popleft()
        count = max((len(frames) for frames in streams), default=0)
        active = count > 0
        actual = count / window
        return actual, display_refresh_rate(self._owner), active

    def start_frame_profile(self, duration_seconds: float = 30.0) -> None:
        """Capture paint cadence, request latency and GUI event-loop stalls."""
        self._stop_profile_probes()
        self._profile_started_at = time.monotonic()
        self._profile_duration_s = max(1.0, float(duration_seconds))
        self._profile_frame_timestamps.clear()
        self._profile_paint_timestamps.clear()
        self._profile_probe_intervals_ms.clear()
        self._profile_probe_last = self._profile_started_at
        _reset_performance_diagnostics(self._profile_started_at, self._profile_duration_s)


        self._profile_probe_timer.setInterval(max(4, min(10, self.interval_ms())))
        self._profile_probe_timer.start()


        self._profile_dispatcher = QtCore.QAbstractEventDispatcher.instance(self.thread())
        if self._profile_dispatcher is not None:
            self._profile_dispatcher.awake.connect(self._profile_awake)
            self._profile_dispatcher.aboutToBlock.connect(self._profile_about_to_block)
        gc.callbacks.append(self._profile_gc)
        application = QtWidgets.QApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(self._stop_profile_probes)

    def _stop_profile_probes(self) -> None:
        self._profile_probe_timer.stop()
        had_probe = self._profile_gc in gc.callbacks
        if self._profile_dispatcher is not None:
            self._profile_dispatcher.awake.disconnect(self._profile_awake)
            self._profile_dispatcher.aboutToBlock.disconnect(self._profile_about_to_block)
            self._profile_dispatcher = None
        if had_probe:
            gc.callbacks.remove(self._profile_gc)
            application = QtWidgets.QApplication.instance()
            if application is not None:
                application.aboutToQuit.disconnect(self._stop_profile_probes)
        self._profile_gc_started.clear()
        self._profile_awake_at = 0.0

    def _profile_awake(self) -> None:
        self._profile_awake_at = time.perf_counter()

    def _profile_about_to_block(self) -> None:
        if self._profile_awake_at:
            record_performance_timing("qt.gui_active_turn_ms", (time.perf_counter() - self._profile_awake_at) * 1000)
            self._profile_awake_at = 0.0

    def _profile_gc(self, phase, info) -> None:
        generation = int(info.get("generation", 0))
        if phase == "start":
            self._profile_gc_started[generation] = time.perf_counter()
        else:
            started = self._profile_gc_started.pop(generation, None)
            if started is not None:
                record_performance_timing(f"python.gc.gen{generation}_ms", (time.perf_counter() - started) * 1000)

    def _profile_probe_tick(self) -> None:
        started = self._profile_started_at
        duration = self._profile_duration_s
        if started <= 0.0 or duration <= 0.0:
            self._stop_profile_probes()
            return
        now = time.monotonic()
        if now > started + duration:
            self._stop_profile_probes()
            return
        previous = self._profile_probe_last
        self._profile_probe_last = now
        if previous <= 0.0 or now <= previous:
            return
        elapsed_ms = (now - previous) * 1000.0
        self._profile_probe_intervals_ms.append(elapsed_ms)
        record_performance_timing("qt.event_loop_interval_ms", elapsed_ms)
        expected_ms = float(max(1, self._profile_probe_timer.interval()))
        lateness_ms = max(0.0, elapsed_ms - expected_ms)
        record_performance_timing("qt.event_loop_lateness_ms", lateness_ms)
        if elapsed_ms >= max(25.0, expected_ms * 3.0):
            record_performance_count("qt.event_loop_stalls")

    def record_chart_paint(self, source: object, timestamp: float | None = None) -> None:
        """Record one actual chart viewport paint during an active profile window.

        PresentationClock callbacks are scheduling transactions, not rendered frames:
        Qt/PyQtGraph can repaint a viewport independently.  Keeping samples per
        viewport also prevents near-simultaneous paints from multiple charts from
        being misread as sub-millisecond frame intervals / ~1000 FPS.
        """
        stamp = time.monotonic() if timestamp is None else float(timestamp)
        key = id(source)
        recent = self._paint_timestamps.setdefault(key, deque())
        recent.append(stamp)
        while recent and recent[0] < stamp - 2.0:
            recent.popleft()
        started = self._profile_started_at
        duration = self._profile_duration_s
        if started <= 0.0 or duration <= 0.0:
            return
        if not (started <= stamp <= started + duration):
            return
        key = id(source)
        self._profile_paint_timestamps.setdefault(key, []).append(stamp)
        requested_at = _PROFILE_PENDING_PAINT_REQUESTS.pop(key, None)
        if requested_at is not None and stamp >= requested_at:
            latency_ms = (stamp - requested_at) * 1000.0
            _PROFILE_REQUESTED_PAINT_TIMESTAMPS.setdefault(key, []).append(stamp)
            _PROFILE_PAINT_REQUEST_LATENCIES.setdefault(key, []).append(latency_ms)
            record_performance_timing("qt.paint_request_latency_ms", latency_ms)
            record_performance_count("qt.requested_paints")
        else:
            record_performance_count("qt.unsolicited_paints")

    @staticmethod
    def _percentile(values: list[float], fraction: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = int(round((len(ordered) - 1) * fraction))
        return ordered[max(0, min(len(ordered) - 1, index))]

    def frame_profile(self) -> dict[str, Any]:
        """Return the active/completed capture without changing presentation behavior."""
        started = self._profile_started_at
        duration = self._profile_duration_s
        if started <= 0.0 or duration <= 0.0:
            return {"active": False, "completed": False}
        now = time.monotonic()
        elapsed = min(duration, max(0.0, now - started))
        active = now - started < duration


        paint_items = list(self._profile_paint_timestamps.items())
        if paint_items:
            source_key, frames = max(paint_items, key=lambda item: len(item[1]))
        else:
            source_key, frames = 0, self._profile_frame_timestamps
        frame_ms = [
            (current - previous) * 1000.0
            for previous, current in zip(frames, frames[1:])
            if current > previous
        ]
        average_frame_ms = sum(frame_ms) / len(frame_ms) if frame_ms else 0.0
        p99_ms = self._percentile(frame_ms, 0.99)
        event_loop_ms = list(self._profile_probe_intervals_ms)
        event_loop_avg_ms = (
            sum(event_loop_ms) / len(event_loop_ms) if event_loop_ms else 0.0
        )
        request_latency_ms = list(_PROFILE_PAINT_REQUEST_LATENCIES.get(source_key, ()))
        requested_paints = list(_PROFILE_REQUESTED_PAINT_TIMESTAMPS.get(source_key, ()))
        surfaces = []
        for key, samples in paint_items:
            intervals = [(current - previous) * 1000 for previous, current in zip(samples, samples[1:]) if current > previous]
            widget = self._frame_sources.get(key)
            surfaces.append({
                'name': widget.objectName() or type(widget).__name__ if widget is not None else str(key),
                'frame_count': len(samples), 'paint_rate_fps': len(samples) / elapsed if elapsed else 0.0,
                'frame_p95_ms': self._percentile(intervals, .95), 'frame_p99_ms': self._percentile(intervals, .99),
                'frame_max_ms': max(intervals, default=0.0),
                'request_latency_p99_ms': self._percentile(list(_PROFILE_PAINT_REQUEST_LATENCIES.get(key, ())), .99),
            })
        if not active:
            self._stop_profile_probes()
        return {
            "active": active,
            "completed": not active,
            "duration_s": duration,
            "elapsed_s": elapsed,
            "remaining_s": max(0.0, duration - elapsed),
            "frame_count": len(frames),
            "sample_source": "chart_paint" if paint_items else "presentation_clock",
            "chart_streams": len(paint_items),
            "surfaces": surfaces,


            "avg_fps": len(frames) / elapsed if elapsed > 0.0 else 0.0,
            "paint_rate_fps": len(frames) / elapsed if elapsed > 0.0 else 0.0,
            "requested_paint_count": len(requested_paints),
            "target_fps": display_refresh_rate(self._owner),
            "frame_avg_ms": average_frame_ms,
            "frame_p50_ms": self._percentile(frame_ms, 0.50),
            "frame_p95_ms": self._percentile(frame_ms, 0.95),
            "frame_p99_ms": p99_ms,
            "frame_max_ms": max(frame_ms, default=0.0),
            "one_percent_low_fps": 1000.0 / p99_ms if p99_ms > 0.0 else 0.0,
            "event_loop_probe_count": len(event_loop_ms),
            "event_loop_avg_ms": event_loop_avg_ms,
            "event_loop_p95_ms": self._percentile(event_loop_ms, 0.95),
            "event_loop_p99_ms": self._percentile(event_loop_ms, 0.99),
            "event_loop_max_ms": max(event_loop_ms, default=0.0),
            "event_loop_effective_hz": 1000.0 / event_loop_avg_ms if event_loop_avg_ms > 0.0 else 0.0,
            "paint_latency_avg_ms": (sum(request_latency_ms) / len(request_latency_ms)) if request_latency_ms else 0.0,
            "paint_latency_p95_ms": self._percentile(request_latency_ms, 0.95),
            "paint_latency_p99_ms": self._percentile(request_latency_ms, 0.99),
            "paint_latency_max_ms": max(request_latency_ms, default=0.0),
            "diagnostics": _performance_diagnostic_summary(),
        }

    def _flush(self) -> None:
        if self._frame_presenter is not None:
            if self._awaiting_paint or self._awaiting_swap:
                kind = "paint" if self._awaiting_paint else "swap"


                self._requested |= self._awaiting_paint
                self._awaiting_paint = self._awaiting_swap = False
                record_performance_count(f"qt.{kind}_watchdog_expired")
            if self._continuous or self._requested:
                self._schedule_frame(immediate=True)
            return
        if not self._requested:
            return
        frame_time = time.monotonic()
        period = 1.0 / display_refresh_rate(self._owner)


        previous_deadline = self._next_frame_deadline
        if previous_deadline <= 0.0:
            self._next_frame_deadline = frame_time + period
        else:
            steps = max(1, math.floor((frame_time - previous_deadline) / period) + 1)
            self._next_frame_deadline = previous_deadline + steps * period
        self._commit_frame(frame_time)
        if self._requested:
            self.request()

    def _commit_frame(self, frame_time: float) -> None:
        self._requested = False
        self._frame_timestamps.append(frame_time)
        if (
            self._profile_started_at > 0.0
            and self._profile_started_at <= frame_time <= self._profile_started_at + self._profile_duration_s
        ):
            self._profile_frame_timestamps.append(frame_time)
        cutoff = frame_time - 2.0
        while self._frame_timestamps and self._frame_timestamps[0] < cutoff:
            self._frame_timestamps.popleft()
        self._flushing = True
        started = time.perf_counter() if performance_profile_active() else 0.0
        try:
            self.interaction_frame.emit(frame_time)
            self.frame.emit(frame_time)
        finally:
            self._flushing = False
            if started:
                record_performance_timing("qt.frame_commit_ms", (time.perf_counter() - started) * 1000)
