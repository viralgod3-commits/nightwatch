import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace

import httpx
import pytest
from PySide6 import QtCore, QtWidgets

from nightwatch.models import Candle
from nightwatch.market.data import _SocketParserWorker, _DepthParserWorker
from nightwatch.market.recording import commit_market_events, spool_market_events
from nightwatch.networking.binance import ApiTask, _AsyncHttpRuntime


@pytest.mark.parametrize('worker_type', [_SocketParserWorker, _DepthParserWorker])
def test_parser_drain_yields_and_eventually_drains(qapp, monkeypatch, worker_type):
    worker = worker_type()
    parsed = []
    monkeypatch.setattr(worker, 'parse', lambda *args: parsed.append(args))
    for _ in range(100):
        if worker_type is _SocketParserWorker:
            worker.enqueue('market', 1, '{}', 'BTCUSDT', '1m', (), frozenset(), time.perf_counter() * 1000)
        else:
            worker.enqueue(1, '{}', 'BTCUSDT', None)
    worker.drain()
    assert 0 < len(parsed) <= worker.DRAIN_BATCH_SIZE
    assert worker._ingress
    deadline = time.monotonic() + 1
    while worker._ingress and time.monotonic() < deadline:
        qapp.processEvents()
    assert len(parsed) == 100
    assert worker._ingress_bytes == 0
    worker.deleteLater()


def test_parser_oversized_message_triggers_boundary_without_queueing(qapp):
    worker = _SocketParserWorker()
    problems = []
    worker.backpressure.connect(lambda *args: problems.append(args))
    worker.enqueue('market', 1, 'x' * (worker.MAX_INGRESS_BYTES // 4 + 1), 'BTCUSDT', '1m', (), frozenset(), 0)
    assert not worker._ingress
    worker.drain()
    assert problems
    assert worker._ingress_bytes == 0
    worker.deleteLater()


def test_cancelled_task_never_runs_or_retries(qapp):
    calls = []
    task = ApiTask(lambda: calls.append('ran'), defer_rate_waits=True)
    task.cancel()
    task.run()
    task.signals.schedule_retry(.05)
    assert not calls and not task.retry_timer.isActive()


def test_binary_asset_limit_is_enforced_during_stream():
    chunks_read = []
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index in range(100):
                chunks_read.append(index)
                yield b'x' * 1024
    async def scenario():
        runtime = _AsyncHttpRuntime()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream()))) as client:
            runtime._client = client
            with pytest.raises(RuntimeError, match='exceeds'):
                await runtime.request_bytes('https://example.test/icon', max_bytes=2048)
    asyncio.run(scenario())
    assert len(chunks_read) < 100


def test_recorder_spool_is_replayed_and_removed_only_after_commit(tmp_path):
    class Db:
        path = str(tmp_path / 'market.sqlite3')
        def insert_market_events(self, events):
            self.saved = events
    db = Db()
    rows = [('BTCUSDT', 'mark', 1, {'p': 100})]
    spool_market_events(db, rows)
    path = tmp_path / 'market.sqlite3.recorder-spool.json'
    assert path.exists()
    commit_market_events(db, [('BTCUSDT', 'mark', 2, {'p': 101})])
    assert len(db.saved) == 2 and not path.exists()


def test_recorder_failed_commit_preserves_entire_spool(tmp_path):
    db = SimpleNamespace(path=str(tmp_path / 'market.sqlite3'))
    db.insert_market_events = lambda events: (_ for _ in ()).throw(sqlite3.OperationalError('database locked'))
    rows = [('BTCUSDT', 'mark', index, {'p': index}) for index in range(750)]
    spool_market_events(db, rows)
    with pytest.raises(sqlite3.OperationalError):
        commit_market_events(db, [])
    assert len(json.loads((tmp_path / 'market.sqlite3.recorder-spool.json').read_text())) == 750


def test_analytics_waits_use_a_separate_qt_pool(qapp):
    from nightwatch.chart.analysis import LatestJob
    job = LatestJob(QtCore.QThreadPool.globalInstance())
    assert job.pool is not QtCore.QThreadPool.globalInstance()
    job.close()
    job.deleteLater()


def test_frame_profile_reports_every_surface(qapp):
    from nightwatch.presentation import PresentationClock
    owner = QtWidgets.QWidget()
    clock = PresentationClock(owner)
    panes = [QtWidgets.QWidget(owner), QtWidgets.QWidget(owner)]
    for index, pane in enumerate(panes):
        pane.setObjectName(f'pane-{index}')
        clock.register_frame_source(pane)
    clock.start_frame_profile(1)
    start = clock._profile_started_at
    for index in range(10):
        clock.record_chart_paint(panes[0], start + index * .01)
    for index in range(3):
        clock.record_chart_paint(panes[1], start + index * .04)
    surfaces = clock.frame_profile()['surfaces']
    assert len(surfaces) == 2
    assert sorted(surface['frame_count'] for surface in surfaces) == [3, 10]
    clock._stop_profile_probes()
    owner.deleteLater()


def test_order_flow_failure_schedules_restart_and_discards_old_inputs(qapp, monkeypatch):
    from nightwatch.orderbook.backend import OrderFlowRuntime, _OrderBookProcessLink
    monkeypatch.setattr(_OrderBookProcessLink, 'enable', lambda *a: None)
    runtime = OrderFlowRuntime('BTCUSDT')
    old = runtime._link
    runtime._failed('worker exited')
    assert runtime._restart_pending and runtime._restart_timer.isActive()
    runtime.reset_model(2, 'ETHUSDT', .01, 1_000_000)
    runtime.add_trade_batch(2, ({'s': 'ETHUSDT'},))
    runtime._restart_timer.stop()
    runtime._restart()
    assert runtime._link is not old
    assert runtime._link._pending[0] == ('reset_model', (2, 'ETHUSDT', .01, 1_000_000))
    assert not any(name == 'add_trade_batch' for name, _ in runtime._link._pending)
    runtime.stop_transport()
    runtime.deleteLater()
