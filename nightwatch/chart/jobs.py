"""Bounded latest-state worker mailboxes, independent of presentation cadence."""
from __future__ import annotations

import logging
from PySide6 import QtCore

log = logging.getLogger(__name__)


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
    """One running + one replaceable pending job; no unbounded executor queue.

    A generation/key is checked again by the consumer before adoption. Failed
    jobs are reported once, never put back into an automatic retry loop.
    """
    ready = QtCore.Signal(object, object)
    failed = QtCore.Signal(object, str)

    def __init__(self, pool, parent=None):
        super().__init__(parent)
        self.pool = pool
        self.running = None
        self.pending = None
        self.wanted = None

    def submit(self, key, function, *args):
        if key == self.wanted:
            return
        self.wanted = key
        request = (key, function, args)
        if self.running is None:
            self._start(request)
        else:
            self.pending = request

    def invalidate(self):
        self.wanted = None
        self.pending = None

    def _start(self, request):
        self.running = _Job(*request)
        self.running.signals.finished.connect(self._finished, QtCore.Qt.ConnectionType.QueuedConnection)
        self.pool.start(self.running)

    @QtCore.Slot(object, object, object)
    def _finished(self, key, result, error):
        self.running = None
        if key == self.wanted:
            if error is None:
                self.ready.emit(key, result)
            else:
                self.failed.emit(key, error)
        pending, self.pending = self.pending, None
        if pending is not None:
            self._start(pending)
