"""Regression coverage for sector refreshes and coordinated console shutdown."""
import signal

from nightwatch.entrypoint import _console_interrupt_shutdown
from nightwatch.leadership import _SectorOverviewSectorTile
from nightwatch.theme import DEFAULT_THEME_NAME, THEMES, ui_palette


def test_sector_tile_refresh_uses_existing_volume_bars(qapp):
    tile = _SectorOverviewSectorTile('AI', ui_palette(THEMES[DEFAULT_THEME_NAME]))
    try:
        metrics = dict(performance=2.5, volume_share=0.25, outperformers=3,
                       covered=4, members=5, volume_history_covered=4,
                       volume_bars=[1.0, 2.0, None])
        for _ in range(3):
            tile.update_data(metrics, '24h')
        assert '2.5' in tile.performance.text()
        assert tile.members.text() == '3 / 4 outperform BTC'
        assert '4/5 pairs' in tile.bars.toolTip()
        tile.update_data({}, '1h')
        assert tile.members.text() == '0 / 0 outperform BTC'
        assert '0/0 pairs' in tile.bars.toolTip()
    finally:
        tile.close()


def test_console_interrupt_defers_close_and_restores_handler(qapp):
    class Window:
        closes = 0

        def close(self):
            self.closes += 1

    window = Window()
    previous = signal.getsignal(signal.SIGINT)
    with _console_interrupt_shutdown(window):
        handler = signal.getsignal(signal.SIGINT)
        handler(signal.SIGINT, None)
        handler(signal.SIGINT, None)
        assert window.closes == 0
        qapp.processEvents()
        assert window.closes == 1
    assert signal.getsignal(signal.SIGINT) is previous


def test_console_interrupt_restores_handler_on_exception():
    previous = signal.getsignal(signal.SIGINT)
    try:
        with _console_interrupt_shutdown(None):
            raise ValueError('event-loop failure')
    except ValueError:
        pass
    assert signal.getsignal(signal.SIGINT) is previous


def test_analysis_worker_ignores_console_interrupt(monkeypatch):
    from nightwatch.chart.analysis import _initialize_analysis_worker
    calls = []
    monkeypatch.setattr(signal, 'signal', lambda *args: calls.append(args))
    _initialize_analysis_worker()
    assert calls == [(signal.SIGINT, signal.SIG_IGN)]


def test_book_worker_ignores_interrupt_and_closes_cooperatively(monkeypatch):
    from nightwatch.orderbook.backend import _orderbook_process_main
    calls = []
    monkeypatch.setattr(signal, 'signal', lambda *args: calls.append(args))

    class Connection:
        closed = False

        def recv(self):
            assert calls == [(signal.SIGINT, signal.SIG_IGN)]
            return None

        def close(self):
            self.closed = True

    class Worker:
        closed = False

        def close(self):
            self.closed = True

    connection, worker = Connection(), Worker()
    _orderbook_process_main(connection, lambda options: worker, {})
    assert connection.closed and worker.closed


def _worker_signal_policy():
    return signal.getsignal(signal.SIGINT) == signal.SIG_IGN


class _SignalPolicyBookWorker:
    def __init__(self, options):
        pass

    def step(self, *args):
        return _worker_signal_policy()

    def close(self):
        pass


def test_spawned_workers_keep_running_after_console_interrupt():
    import multiprocessing
    import os
    from concurrent.futures import ProcessPoolExecutor
    from nightwatch.chart.analysis import _initialize_analysis_worker
    from nightwatch.orderbook.backend import _orderbook_process_main

    context = multiprocessing.get_context('spawn')
    with ProcessPoolExecutor(max_workers=1, mp_context=context,
                             initializer=_initialize_analysis_worker) as pool:
        assert pool.submit(_worker_signal_policy).result(timeout=15)
        if os.name != 'nt':
            for process in pool._processes.values():
                os.kill(process.pid, signal.SIGINT)
        assert pool.submit(_worker_signal_policy).result(timeout=15)

    parent, child = context.Pipe()
    process = context.Process(target=_orderbook_process_main,
                              args=(child, _SignalPolicyBookWorker, {}))
    process.start()
    child.close()
    try:
        parent.send(())
        assert parent.poll(15) and parent.recv() is True
        if os.name != 'nt':
            os.kill(process.pid, signal.SIGINT)
        parent.send(())
        assert parent.poll(15) and parent.recv() is True
        parent.send(None)
        process.join(timeout=10)
        assert process.exitcode == 0
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
