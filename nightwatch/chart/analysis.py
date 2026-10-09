"""Headless analysis process. Python indicator loops cannot monopolize Qt's GIL."""
from __future__ import annotations

from collections import OrderedDict
import numpy as np

from ..models import Candle, chart_y_array
from ..indicators import (PriceStudyCache, volume_profile, profile_levels,
                          build_major_level_candidates, rank_major_level_candidates,
                          detect_auto_fibonacci_candidates)
from .preparation import RangeExtrema, decimate_series

_states = OrderedDict()


def _candles(matrix):
    return [Candle(*map(float, row)) for row in matrix]


def _remember_state(token, state):
    if state is not None:
        _states[token] = state
    while len(_states) > 12:
        _states.popitem(last=False)


def _delta_cache_miss(token, state):
    _remember_state(token, state)
    return {"__cache_miss__": True}


def _apply_indicator_source_update(source, update):
    """Apply one compact GUI->analysis source update without replacing ``source``.

    ``source`` identity is part of PriceStudyCache's incremental contract.  Full
    resets replace its contents in place; routine live updates replace only the
    mutable tail, append one candle, or roll one candle at capacity.
    """
    mode = str(update[0]) if update else ""
    if mode == "full":
        matrix = np.asarray(update[1], dtype=np.float64)
        source[:] = _candles(matrix)
        return True, True
    if mode != "delta" or len(update) < 4:
        return False, False

    target_length = max(0, int(update[1]))
    first_time = None if update[2] is None else float(update[2])
    tail = np.asarray(update[3], dtype=np.float64)
    if target_length <= 0:
        if source:
            source.clear()
            return True, True
        return True, False
    if tail.ndim != 2 or tail.shape[1] < 7 or len(tail) == 0:
        return False, False


    if (
        len(source) == target_length
        and source
        and first_time == float(source[0].time)
        and float(tail[-1, 0]) == float(source[-1].time)
    ):
        source[-1] = Candle(*map(float, tail[-1]))
        return True, False


    if (
        len(source) + 1 == target_length
        and source
        and first_time == float(source[0].time)
        and len(tail) >= 2
        and float(tail[-2, 0]) == float(source[-1].time)
    ):
        source[-1] = Candle(*map(float, tail[-2]))
        source.append(Candle(*map(float, tail[-1])))
        return True, False



    if (
        len(source) == target_length
        and len(source) > 1
        and first_time == float(source[1].time)
        and len(tail) >= 2
        and float(tail[-2, 0]) == float(source[-1].time)
    ):
        del source[0]
        source[-1] = Candle(*map(float, tail[-2]))
        source.append(Candle(*map(float, tail[-1])))



        return True, True

    return False, False


def indicator_analysis(token, generation, source_update, requests, first, last, width, logarithmic):
    state = _states.pop(token, None)
    mode = str(source_update[0]) if source_update else ""
    if state is None:
        if mode != "full":
            return _delta_cache_miss(token, None)
        state = [None, [], PriceStudyCache()]

    old_generation, source, cache = state


    if mode != "full" and old_generation is not None:
        try:
            if int(old_generation[0]) != int(generation[0]):
                return _delta_cache_miss(token, state)
        except (IndexError, TypeError, ValueError):
            return _delta_cache_miss(token, state)

    applied, full_reset = _apply_indicator_source_update(source, source_update)
    if not applied:
        return _delta_cache_miss(token, state)
    if full_reset:
        cache.entries.clear()

    state[0] = generation
    _remember_state(token, state)
    first, last = min(first, len(source)), min(last, len(source))
    window = source[first:last]
    times = np.fromiter((c.time for c in window), dtype=float, count=len(window))
    prepared = {}
    for name, settings in requests:
        arrays = cache.values(source, name, settings)
        curves, scale_data, scale_index = [], None, None
        for index, raw in enumerate(arrays):
            values, line_times = np.asarray(raw[first:last], dtype=float), times
            if name == "ATR":
                close = np.fromiter((c.close for c in window), dtype=float, count=len(window))
                values = np.divide(values*100., close, out=np.full_like(values, np.nan), where=close > 0)
                scale_data = (times.copy(), values.copy())
                scale_index = RangeExtrema(values, values)
            elif name != "RSI":
                values = chart_y_array(values, logarithmic)
            if name == "VWAP" and index == 0 and len(times) > 1:
                entry = cache.entries.get(name)
                anchor_state = entry[2] if entry is not None else None
                if anchor_state is not None and len(anchor_state):
                    anchors = np.asarray(anchor_state[0][first:last], dtype=float)
                    if len(anchors) == len(times):
                        breaks = np.flatnonzero(anchors[1:] != anchors[:-1])+1
                        values = np.insert(values, breaks, np.nan)
                        line_times = np.insert(times, breaks, times[breaks])
            curves.append(decimate_series(line_times, values, width))
        prepared[name] = {"curves": tuple(curves), "scale_data": scale_data, "scale_index": scale_index}
    prepared["__source_generation__"] = tuple(generation)
    return prepared


def profile_analysis(matrix, bins, levels):
    centers, volumes = volume_profile(_candles(matrix), bins)
    return centers, volumes, profile_levels(centers, volumes, levels)


def major_level_analysis(frames, price, minimum, maximum):
    evidence = {name: build_major_level_candidates(list(candles), name) for name, candles in frames}
    return rank_major_level_candidates(evidence, price, minimum, maximum)


def fibonacci_analysis(frames, interval, candles, price, zones, maximum):
    return detect_auto_fibonacci_candidates(dict(frames), interval, list(candles), price, list(zones), maximum=maximum)


from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
import multiprocessing
import os
import threading

_lock = threading.Lock()
_executors = {}
_closed = False


def analysis_worker_count():
    from ..compute import cpu_budget
    return cpu_budget().analysis_workers


def _initialize_analysis_worker():
    # The GUI owns Ctrl+C and coordinates cooperative worker shutdown.
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    from ..compute import configure_worker
    configure_worker(analysis=True)



    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                 "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"


    try:
        from threadpoolctl import threadpool_limits
    except ImportError:
        pass
    else:
        threadpool_limits(limits=1)


def run_analysis(function, *args, cache_affinity=False):
    with _lock:
        if _closed:
            raise RuntimeError("Chart analysis is shutting down")
        count = analysis_worker_count()
        # Keep resident research out of the chart-indicator affinity lane while
        # respecting one total CPU budget. Small hosts share an affinity worker.
        lane = ("research" if cache_affinity == "research" and count > 2 else
                "cached" if cache_affinity and count > 1 else "general")
        executor = _executors.get(lane)
        if executor is None:




            executor = ProcessPoolExecutor(
                max_workers=1 if lane != "general" else max(1, count - min(2, count - 1)),
                mp_context=multiprocessing.get_context("spawn"),
                initializer=_initialize_analysis_worker,
            )
            _executors[lane] = executor
        try:
            future = executor.submit(function, *args)
        except BrokenProcessPool:
            # A worker can die while idle, so submit itself may fail before a
            # future exists. Retire that lane just as for a failed running job.
            if _executors.get(lane) is executor:
                _executors.pop(lane)
            executor.shutdown(wait=False, cancel_futures=True)
            raise
    try:
        return future.result()
    except BrokenProcessPool:
        with _lock:
            if _executors.get(lane) is executor:
                _executors.pop(lane)
        executor.shutdown(wait=False, cancel_futures=True)
        raise


def shutdown_analysis():
    global _closed
    with _lock:
        _closed = True
        executors = tuple(_executors.values())
        _executors.clear()
    for executor in executors:
        executor.shutdown(wait=False, cancel_futures=True)


import logging
from PySide6 import QtCore

log = logging.getLogger(__name__)
_analysis_qt_pool = None


def analysis_task_pool():
    """Keep process-future waits out of the shared HTTP/SQLite task pool."""
    global _analysis_qt_pool
    if _analysis_qt_pool is None:
        _analysis_qt_pool = QtCore.QThreadPool()
        _analysis_qt_pool.setMaxThreadCount(analysis_worker_count() + 1)
        _analysis_qt_pool.setExpiryTimeout(30_000)
    return _analysis_qt_pool


class _Signals(QtCore.QObject):
    finished = QtCore.Signal(object, object, object)


class _Job(QtCore.QRunnable):
    def __init__(self, key, function, args):
        super().__init__()
        self.key, self.function, self.args = key, function, args
        self.signals = _Signals()

    def run(self):
        try:
            result, error = self.function(*self.args), None
        except Exception as exc:
            log.exception("Chart preparation failed (%r)", self.key)
            result, error = None, str(exc)
        self.signals.finished.emit(self.key, result, error)


class LatestJob(QtCore.QObject):
    """One running and one replaceable pending request, with explicit epochs.

    Inputs must be immutable snapshots. A receiver may opt into intermediate
    results for a continuous stream, then validate its own market/configuration
    key; otherwise only the newest requested result is delivered.
    """
    ready = QtCore.Signal(object, object)
    failed = QtCore.Signal(object, str)

    def __init__(self, pool, parent=None, *, accept_intermediate=False, priority=0):
        super().__init__(parent)
        self.pool = analysis_task_pool() if pool is QtCore.QThreadPool.globalInstance() else pool
        self.running = None
        self.pending = None
        self.wanted = None
        self._epoch = 0
        self._closed = False
        self._accept_intermediate = bool(accept_intermediate)
        self._priority = int(priority)

    def submit(self, key, function, *args):
        if self._closed or key == self.wanted:
            return
        self.wanted = key
        request = ((self._epoch, key), function, args)
        if self.running is None:
            self._start(request)
        elif self.running.key == request[0]:
            self.pending = None
        else:
            self.pending = request

    def invalidate(self):
        self._epoch += 1
        self.wanted = None
        self.pending = None

    def close(self):
        self._closed = True
        self.invalidate()

    def _start(self, request):
        self.running = _Job(*request)
        self.running.signals.finished.connect(
            self._finished, QtCore.Qt.ConnectionType.QueuedConnection
        )
        self.pool.start(self.running, self._priority)

    @QtCore.Slot(object, object, object)
    def _finished(self, ticket, result, error):
        self.running = None
        epoch, key = ticket
        deliver = (not self._closed and epoch == self._epoch
                   and (key == self.wanted or self._accept_intermediate))
        pending, self.pending = self.pending, None


        if pending is not None and not self._closed:
            self._start(pending)
        if deliver:
            if error is None:
                self.ready.emit(key, result)
            else:
                if key == self.wanted:
                    self.wanted = None
                self.failed.emit(key, error)
