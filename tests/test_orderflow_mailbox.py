import threading

from PySide6 import QtCore


class _OrderFlowReceiver(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self.events = []
        self.callback_threads = []

    @QtCore.Slot(int, object)
    def snapshot(self, generation, value):
        self.events.append(("snapshot_ready", generation, value))
        self.callback_threads.append(QtCore.QThread.currentThread())

    @QtCore.Slot(int, object)
    def microstructure(self, generation, value):
        self.events.append(("microstructure_ready", generation, value))

    @QtCore.Slot(int, object)
    def diagnostic(self, generation, value):
        self.events.append(("diagnostic_ready", generation, value))

    @QtCore.Slot(int, str)
    def failed(self, generation, message):
        self.events.append(("failed", generation, message))


class _OrderFlowPublisher(QtCore.QObject):
    publish = QtCore.Signal(object)


def _runtime(qapp, monkeypatch):
    from nightwatch.orderbook.backend import OrderFlowRuntime, _OrderBookProcessLink

    monkeypatch.setattr(_OrderBookProcessLink, "enable", lambda *_args: None)
    runtime = OrderFlowRuntime("BTCUSDT")
    receiver = _OrderFlowReceiver()
    runtime.snapshot_ready.connect(receiver.snapshot, QtCore.Qt.ConnectionType.QueuedConnection)
    runtime.microstructure_ready.connect(
        receiver.microstructure, QtCore.Qt.ConnectionType.QueuedConnection
    )
    runtime.diagnostic_ready.connect(
        receiver.diagnostic, QtCore.Qt.ConnectionType.QueuedConnection
    )
    runtime.failed.connect(receiver.failed, QtCore.Qt.ConnectionType.QueuedConnection)
    return runtime, receiver


def _pump(qapp):
    # Mailbox wakeups emit the retained public Qt signals, which then enqueue
    # their existing receiver callbacks. Give both queued stages a turn.
    for _ in range(4):
        qapp.processEvents()


def test_paused_gui_coalesces_a_thousand_relay_bursts_to_latest_values(qapp, monkeypatch):
    runtime, receiver = _runtime(qapp, monkeypatch)
    mailbox = runtime._presentation_mailbox
    burst_count = 1000

    def produce_burst():
        for sequence in range(burst_count):
            runtime._deliver(
                {
                    "events": [
                        ("snapshot_ready", 0, {"sequence": sequence}),
                        ("microstructure_ready", 0, {"sequence": sequence}),
                        ("diagnostic_ready", 0, {"sequence": sequence}),
                    ]
                }
            )

    # The GUI thread is deliberately not pumping events during this producer
    # burst, matching a temporarily blocked GUI event loop.
    producer = threading.Thread(target=produce_burst)
    producer.start()
    producer.join(timeout=5)
    assert not producer.is_alive()

    assert mailbox.wakeups_posted == 1
    assert mailbox.coalesced_counts == {
        "snapshot_ready": burst_count - 1,
        "microstructure_ready": burst_count - 1,
        "diagnostic_ready": burst_count - 1,
    }

    _pump(qapp)
    assert receiver.events == [
        ("snapshot_ready", 0, {"sequence": burst_count - 1}),
        ("microstructure_ready", 0, {"sequence": burst_count - 1}),
        ("diagnostic_ready", 0, {"sequence": burst_count - 1}),
    ]
    assert mailbox.emitted_counts == {
        "snapshot_ready": 1,
        "microstructure_ready": 1,
        "diagnostic_ready": 1,
        "failed": 0,
    }
    runtime.stop_transport()
    runtime.deleteLater()


def test_generation_reset_discards_stale_values_and_shutdown_keeps_failures(qapp, monkeypatch):
    runtime, receiver = _runtime(qapp, monkeypatch)
    runtime._post = lambda *_args: None

    runtime._deliver({"events": [("snapshot_ready", 0, {"symbol": "BTCUSDT"})]})
    runtime.reset_model(1, "ETHUSDT", 0.01, 1000.0)
    _pump(qapp)
    assert receiver.events == []

    runtime._deliver(
        {
            "events": [
                ("snapshot_ready", 0, {"symbol": "BTCUSDT", "sequence": 2}),
                ("snapshot_ready", 1, {"symbol": "ETHUSDT", "sequence": 3}),
            ]
        }
    )
    runtime._deliver({"events": [("failed", 1, "analysis input failed")]})
    runtime._deliver({"events": [("failed", 1, "worker failed")]})
    # Shutdown clears pending presentation data but keeps already queued
    # failure notifications available through the public signal.
    runtime.stop_transport()
    _pump(qapp)

    assert receiver.events == [
        ("failed", 1, "analysis input failed"),
        ("failed", 1, "worker failed"),
    ]
    assert runtime._presentation_mailbox.wakeups_posted == 2
    runtime.deleteLater()


def test_raw_depth_and_trade_ingress_remains_ordered_and_lossless(qapp, monkeypatch):
    runtime, _receiver = _runtime(qapp, monkeypatch)
    input_count = 500

    for sequence in range(input_count):
        runtime.add_depth(
            0,
            [(100.0 + sequence, 1.0)],
            [(101.0 + sequence, 1.0)],
            sequence,
        )
        runtime.add_trade_batch(0, ({"trade_id": sequence},))

    queued = runtime._link._pending
    assert len(queued) == input_count * 2
    assert all(
        (name, args[0]) == ("add_depth", 0)
        if index % 2 == 0
        else (name, args[0]) == ("add_trade_batch", 0)
        for index, (name, args) in enumerate(queued)
    )
    assert [args[3] for name, args in queued if name == "add_depth"] == list(
        range(input_count)
    )
    assert [args[1][0]["trade_id"] for name, args in queued if name == "add_trade_batch"] == list(
        range(input_count)
    )
    assert runtime._presentation_mailbox.wakeups_posted == 0
    runtime.stop_transport()
    runtime.deleteLater()


def test_failure_bursts_are_preserved_with_a_bounded_drain_batch(qapp, monkeypatch):
    runtime, receiver = _runtime(qapp, monkeypatch)
    mailbox = runtime._presentation_mailbox
    failure_count = 100

    runtime._deliver(
        {
            "events": [
                ("snapshot_ready", 0, {"sequence": "latest"}),
                ("microstructure_ready", 0, {"sequence": "latest"}),
                ("diagnostic_ready", 0, {"sequence": "latest"}),
            ]
            + [
                ("failed", 0, f"failure-{sequence}")
                for sequence in range(failure_count)
            ]
        }
    )
    assert mailbox.wakeups_posted == 1

    for _ in range(10):
        qapp.processEvents()
        if sum(event[0] == "failed" for event in receiver.events) == failure_count:
            break

    failures = [event for event in receiver.events if event[0] == "failed"]
    assert [message for _, _, message in failures] == [
        f"failure-{sequence}" for sequence in range(failure_count)
    ]
    assert mailbox.emitted_counts["failed"] == failure_count
    assert mailbox.max_deliveries_per_drain <= mailbox.MAX_FAILURES_PER_DRAIN + 3
    assert mailbox.emitted_counts["snapshot_ready"] == 1
    assert mailbox.emitted_counts["microstructure_ready"] == 1
    assert mailbox.emitted_counts["diagnostic_ready"] == 1
    assert mailbox.wakeups_posted == 4
    runtime.stop_transport()
    runtime.deleteLater()


def test_producer_consumer_race_retains_the_final_published_snapshot(qapp, monkeypatch):
    runtime, receiver = _runtime(qapp, monkeypatch)
    mailbox = runtime._presentation_mailbox
    started = threading.Event()
    finished = threading.Event()
    final_sequence = 1499

    def produce_concurrently():
        started.set()
        for sequence in range(final_sequence + 1):
            runtime._deliver(
                {"events": [("snapshot_ready", 0, {"sequence": sequence})]}
            )
        finished.set()

    producer = threading.Thread(target=produce_concurrently)
    producer.start()
    assert started.wait(timeout=2)
    # Exercise publication while the GUI drains the single latest-value slot.
    while not finished.is_set():
        qapp.processEvents()
    producer.join(timeout=5)
    _pump(qapp)

    snapshots = [event for event in receiver.events if event[0] == "snapshot_ready"]
    assert snapshots
    assert snapshots[-1] == (
        "snapshot_ready",
        0,
        {"sequence": final_sequence},
    )
    assert mailbox._wake_pending is False
    runtime.stop_transport()
    runtime.deleteLater()


def test_mailbox_stays_on_gui_thread_after_runtime_moves_to_worker(qapp, monkeypatch):
    import shiboken6

    runtime, receiver = _runtime(qapp, monkeypatch)
    mailbox = runtime._presentation_mailbox
    worker = QtCore.QThread()
    publisher = _OrderFlowPublisher()
    delivery_finished = threading.Event()
    runtime_thread = []
    runtime._link.consumed = lambda **_kwargs: (
        runtime_thread.append(QtCore.QThread.currentThread()),
        delivery_finished.set(),
    )
    publisher.publish.connect(runtime._deliver, QtCore.Qt.ConnectionType.QueuedConnection)

    assert mailbox.thread() == qapp.thread()
    runtime.moveToThread(worker)
    assert runtime.thread() == worker
    assert mailbox.thread() == qapp.thread()
    worker.start()

    publisher.publish.emit(
        {"events": [("snapshot_ready", 0, {"sequence": "worker-thread"})]}
    )
    assert delivery_finished.wait(timeout=2)
    _pump(qapp)
    assert receiver.events == [
        ("snapshot_ready", 0, {"sequence": "worker-thread"})
    ]
    assert runtime_thread == [worker]
    assert receiver.callback_threads == [qapp.thread()]

    delivery_finished.clear()
    publisher.publish.emit(
        {"events": [("failed", 0, "queued shutdown failure")]}
    )
    assert delivery_finished.wait(timeout=2)
    runtime.stop_transport()
    _pump(qapp)
    assert receiver.events[-1] == ("failed", 0, "queued shutdown failure")
    assert runtime_thread == [worker, worker]

    destroyed = threading.Event()
    runtime.destroyed.connect(
        lambda *_args: destroyed.set(), QtCore.Qt.ConnectionType.DirectConnection
    )
    runtime.deleteLater()
    assert destroyed.wait(timeout=2)
    for _ in range(4):
        qapp.processEvents()
    if shiboken6.isValid(mailbox):
        QtCore.QCoreApplication.sendPostedEvents(
            mailbox, QtCore.QEvent.Type.DeferredDelete
        )
    assert not shiboken6.isValid(mailbox)
    worker.quit()
    assert worker.wait(2000)


def test_publication_during_gui_drain_schedules_only_one_followup_wakeup(qapp, monkeypatch):
    runtime, receiver = _runtime(qapp, monkeypatch)
    mailbox = runtime._presentation_mailbox
    injected = False

    def publish_during_delivery(generation, _value):
        nonlocal injected
        if injected:
            return
        injected = True
        runtime._deliver(
            {"events": [("snapshot_ready", generation, {"sequence": 1})]}
        )

    runtime.snapshot_ready.connect(
        publish_during_delivery, QtCore.Qt.ConnectionType.DirectConnection
    )
    runtime._deliver({"events": [("snapshot_ready", 0, {"sequence": 0})]})
    _pump(qapp)

    snapshots = [event for event in receiver.events if event[0] == "snapshot_ready"]
    assert snapshots == [
        ("snapshot_ready", 0, {"sequence": 0}),
        ("snapshot_ready", 0, {"sequence": 1}),
    ]
    assert mailbox.wakeups_posted == 2
    assert mailbox._wake_pending is False
    runtime.stop_transport()
    runtime.deleteLater()
