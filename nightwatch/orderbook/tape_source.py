"""GUI subscription bridge; history ingestion belongs to an external worker."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import threading
import time
import uuid
import weakref

from PySide6 import QtCore

from ..models import OrderFlowSnapshot
from .tape import TapeFrame, TapePublisher, TradeTapeHistory


@dataclass(frozen=True, slots=True)
class _SnapshotInput:
    symbol: str
    sequence: int
    large_trade_threshold: float
    recent_prints: tuple
    component_revisions: object


class _StandaloneTapeWorker:
    """Compatibility worker for hosts supplying immutable snapshots directly."""

    MAX_PENDING_BYTES = 16 * 1024 * 1024
    MAX_PENDING_COMMANDS = 128

    def __init__(self, source, symbol):
        self.source = weakref.ref(source)
        self.history = TradeTapeHistory(symbol)
        self.publisher = TapePublisher()
        self.condition = threading.Condition()
        self.pending = deque()
        self.pending_bytes = 0
        self.closed = False
        self.thread = threading.Thread(target=self._run, name='nightwatch-tape-history', daemon=True)
        self.thread.start()

    def submit(self, name, args):
        size = 256 + (len(args[0].recent_prints) * 256 if name == 'snapshot' else 0)
        with self.condition:
            if self.closed:
                return
            if len(self.pending) >= self.MAX_PENDING_COMMANDS or self.pending_bytes + size > self.MAX_PENDING_BYTES:
                self.closed = True
                self.pending.clear()
                self.condition.notify_all()
                self._fail('Trade-tape input exceeded its memory budget; history ingestion stopped')
                return
            self.pending.append((name, args, size))
            self.pending_bytes += size
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.pending.clear()
            self.condition.notify_all()

    def _fail(self, message):
        source = self.source()
        if source is not None:
            try:
                source.failed.emit(message)
            except RuntimeError:
                pass

    def _run(self):
        try:
            while True:
                with self.condition:
                    due = self.publisher.next_at(self.history.state())
                    while not self.closed and not self.pending and time.monotonic() < due:
                        self.condition.wait(None if math.isinf(due) else max(.001, due - time.monotonic()))
                    if self.closed:
                        return
                    commands = list(self.pending)
                    self.pending.clear()
                    self.pending_bytes = 0
                for name, args, _size in commands:
                    if name == 'snapshot':
                        self.history.ingest_snapshot(args[0])
                    elif name == 'set_tape_view':
                        self.publisher.set_view(*args)
                    elif name == 'ack_tape':
                        self.publisher.ack(*args)
                    elif name == 'clear_tape' and args[0] == self.history.symbol:
                        self.history.clear()
                        self.publisher.reset()
                source = self.source()
                if source is None:
                    return
                for frame in self.publisher.publish(self.history.state(), time.monotonic()):
                    source.ready.emit(frame)
        except Exception as error:
            self.close()
            self._fail(f'Trade-tape history worker failed: {error}')


class SharedTradeTapeSource(QtCore.QObject):
    """Small visible-widget registry, with no GUI history or snapshot scans."""

    requested = QtCore.Signal(str, object)
    ready = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, runtime=None, *, symbol='BTCUSDT', parent=None):
        super().__init__(parent)
        self._widgets = weakref.WeakValueDictionary()
        self._runtime = weakref.ref(runtime) if runtime is not None else None
        self._closed = False
        self._worker = None
        self.delivered_frames = self.applied_records = self.stale_frames = 0
        self.ready.connect(self._deliver, QtCore.Qt.ConnectionType.QueuedConnection)
        self.failed.connect(self._failed, QtCore.Qt.ConnectionType.QueuedConnection)
        if runtime is not None:
            self.requested.connect(runtime.tape_command, QtCore.Qt.ConnectionType.QueuedConnection)
            runtime.tape_ready.connect(self._runtime_frame, QtCore.Qt.ConnectionType.QueuedConnection)
            runtime.destroyed.connect(self.deleteLater)
        else:
            self._worker = _StandaloneTapeWorker(self, symbol)
        self.destroyed.connect(lambda *_: self.close())
        app = QtCore.QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.close)

    def command(self, name, args):
        if self._closed:
            return
        if self._worker is not None:
            self._worker.submit(name, args)
        else:
            self.requested.emit(name, args)

    def register(self, widget):
        consumer = uuid.uuid4().hex
        self._widgets[consumer] = widget
        widget.destroyed.connect(lambda *_: self.unregister(consumer))
        return consumer

    def unregister(self, consumer):
        self._widgets.pop(consumer, None)
        self.command('set_tape_view', (consumer, 0, '', 'LARGE', False))

    def refresh(self, widget):
        self.command('set_tape_view', (widget._tape_consumer, widget._tape_token, widget.symbol,
                                      widget.mode(), widget._active and widget.isVisible()))

    def ingest_snapshot(self, snapshot):
        # External runtime sources consume accepted trades directly. A host may
        # keep forwarding DOM snapshots, but they must not duplicate ingestion.
        if self._worker is not None and isinstance(snapshot, OrderFlowSnapshot):
            incoming = _SnapshotInput(snapshot.symbol, snapshot.sequence, snapshot.large_trade_threshold,
                                      snapshot.recent_prints, snapshot.component_revisions)
            self.command('snapshot', (incoming,))

    @QtCore.Slot(int, object)
    def _runtime_frame(self, generation, frame):
        # The runtime mailbox generation-checks before this GUI signal. A view
        # token additionally rejects queued frames after hide/mode/market changes.
        runtime = self._runtime() if self._runtime is not None else None
        if runtime is not None and generation == runtime._generation:
            self._deliver(frame)

    @QtCore.Slot(object)
    def _deliver(self, frame):
        if self._closed or not isinstance(frame, TapeFrame):
            return
        widget = self._widgets.get(frame.consumer)
        if widget is None:
            self.unregister(frame.consumer)
            return
        self.delivered_frames += 1
        self.applied_records += len(frame.patch.upserts)
        if not widget._receive_tape_frame(frame):
            self.stale_frames += 1

    @QtCore.Slot(str)
    def _failed(self, message):
        import logging
        logging.getLogger(__name__).error('%s', message)
        for widget in tuple(self._widgets.values()):
            widget.status.setText('Trade history unavailable')

    def close(self):
        self._closed = True
        if self._worker is not None:
            self._worker.close()
