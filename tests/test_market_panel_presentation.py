"""Secondary market views share chart frames without releasing pixels early."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6 import QtCore, QtGui, QtWidgets

from nightwatch.orderbook import backend, orderbook_ui as ui
from nightwatch.models import OrderFlowSnapshot, OrderFlowTradePrint
from nightwatch.presentation import PresentationClock


class Clock(QtCore.QObject):
    interaction_frame = QtCore.Signal(float)

    def __init__(self):
        super().__init__()
        self.requests = 0

    def request(self, **kwargs):
        self.requests += 1


class Link(QtCore.QObject):
    ready = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, factory, options, parent=None, **kwargs):
        super().__init__(parent)
        self.commands, self.acks = [], []
        self.enabled = False

    def submit(self, name, value):
        self.commands.append((name, value))

    def enable(self, active):
        self.enabled = active

    def consumed(self, *, lease=None):
        self.acks.append(lease)

    def close(self):
        self.enabled = False


def settle(app):
    for _ in range(4):
        app.processEvents()


def print_(sequence):
    return OrderFlowTradePrint(
        sequence=sequence, event_time_ms=1700000000000 + sequence,
        received_monotonic=sequence / 10, price=100.0, quantity=1.0,
        notional=100.0, aggressor_side='BUY', normal_notional=100.0,
        rpi_notional=0.0, relative_size=1.0, salience_class=1,
    )


def snapshot(sequence):
    return OrderFlowSnapshot(
        symbol='BTCUSDT', sequence=sequence, generated_monotonic=sequence / 10,
        ready=True, live=True, recent_prints=(print_(sequence),),
        bbo_source='depth', depth_age_seconds=0.0, bbo_age_seconds=0.0,
        trade_age_seconds=0.0,
        large_trade_threshold=10,
    )


@pytest.fixture
def canvas(qapp, monkeypatch):
    monkeypatch.setattr(backend, '_OrderBookProcessLink', Link)
    widget = ui.OrderFlowDomCanvas({})
    widget.resize(360, 480)
    widget.show()
    settle(qapp)
    yield widget
    widget.close()
    widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def raster(widget, *, market_epoch=None):
    image = QtGui.QImage(widget.size(), QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QtGui.QColor('#123456'))
    return dict(
        worker_pid=123, epoch=widget._display_epoch,
        market_epoch=widget._market_epoch if market_epoch is None else market_epoch,
        frame=('test-image', 0, image.width(), image.height(), 1.0),
        pixels=SimpleNamespace(images=[image]), geometry=dict(widget._geometry),
        prices=((), ()), sequence=1, layout=widget.layout_state(),
    )


def test_dom_adopts_at_chart_frame_and_acknowledges_only_after_paint(canvas, qapp):
    clock = Clock()
    canvas.set_presentation_clock(clock)
    canvas.set_interaction_priority(True)
    result = raster(canvas)
    canvas._adopt_raster(result)
    assert clock.requests == 1
    assert canvas._display_frame is None and canvas._process_link.acks == []
    # An incidental paint of the previous image must not free incoming pixels.
    image = QtGui.QImage(canvas.size(), QtGui.QImage.Format.Format_ARGB32)
    canvas.render(image)
    assert canvas._process_link.acks == []
    clock.interaction_frame.emit(1.0)
    assert canvas._display_frame is result and canvas._process_link.acks == []
    settle(qapp)
    assert canvas._process_link.acks == [('test-image', 0)]


@pytest.mark.parametrize('finish', ['end_interaction', 'detach_clock'])
def test_dom_flushes_held_frame_without_another_clock_tick(canvas, qapp, finish):
    canvas.set_presentation_clock(Clock())
    canvas.set_interaction_priority(True)
    result = raster(canvas)
    canvas._adopt_raster(result)
    if finish == 'end_interaction':
        canvas.set_interaction_priority(False)
    else:
        canvas.set_presentation_clock(None)
    settle(qapp)
    assert canvas._display_frame is result
    assert canvas._process_link.acks == [('test-image', 0)]


@pytest.mark.parametrize('boundary', ['hide', 'reset', 'stale_epoch'])
def test_dom_boundary_discards_pending_frame_and_releases_lease(canvas, qapp, boundary):
    clock = Clock()
    canvas.set_presentation_clock(clock)
    canvas.set_interaction_priority(True)
    result = raster(canvas, market_epoch=canvas._market_epoch - 1 if boundary == 'stale_epoch' else None)
    canvas._adopt_raster(result)
    if boundary == 'hide':
        canvas.hide()
    elif boundary == 'reset':
        canvas.reset()
    settle(qapp)
    clock.interaction_frame.emit(1.0)
    assert canvas._pending_raster is None
    assert canvas._display_frame is None
    assert canvas._process_link.acks == [None]


def test_dom_priority_is_part_of_worker_configuration(canvas):
    canvas.set_interaction_priority(True)
    configs = [value[1] for name, value in canvas._process_link.commands if name == 'config']
    assert configs[-1]['interaction_priority'] is True
    canvas.set_interaction_priority(False)
    configs = [value[1] for name, value in canvas._process_link.commands if name == 'config']
    assert configs[-1]['interaction_priority'] is False


@pytest.fixture
def tape(qapp):
    widget = ui.TradesTapeWidget({})
    widget.set_market('BTCUSDT', tick_size=.1)
    widget.resize(480, 240)
    widget.show()
    widget.set_panel_active(True)
    settle(qapp)
    yield widget
    widget.close()
    widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def test_tape_ingestion_stays_live_and_coalesces_display_to_chart_frame(tape, qapp):
    clock = Clock()
    tape.set_presentation_clock(clock)
    tape.set_interaction_priority(True)
    tape.set_order_flow_snapshot(snapshot(1))
    tape._refresh_table()
    assert tape.model.rowCount() == 0
    tape.set_order_flow_snapshot(snapshot(2))
    assert len(tape._history) == 2  # No prints lost while presentation waits.
    clock.interaction_frame.emit(1.0)
    assert [row[0].sequence for row in tape.model.rows] == [2, 1]
    assert not tape._frame_refresh_pending
    assert clock.requests == 1


@pytest.mark.parametrize('finish', ['end_interaction', 'detach_clock'])
def test_tape_final_refresh_does_not_require_another_chart_frame(tape, finish):
    tape.set_presentation_clock(Clock())
    tape.set_interaction_priority(True)
    tape.set_order_flow_snapshot(snapshot(1))
    tape._refresh_table()
    if finish == 'end_interaction':
        tape.set_interaction_priority(False)
    else:
        tape.set_presentation_clock(None)
    assert tape.model.rowCount() == 1


def test_hidden_tape_cannot_commit_deferred_visible_model_work(tape):
    clock = Clock()
    tape.set_presentation_clock(clock)
    tape.set_interaction_priority(True)
    tape.set_order_flow_snapshot(snapshot(1))
    tape._refresh_table()
    tape.hide()
    clock.interaction_frame.emit(1.0)
    assert tape.model.rowCount() == 0
    assert not tape._frame_refresh_pending


def test_unbound_standalone_tape_preserves_immediate_refresh(tape):
    tape.set_interaction_priority(True)
    tape.set_order_flow_snapshot(snapshot(1))
    tape._refresh_table()
    assert tape.model.rowCount() == 1


def test_actual_shared_clock_commits_both_panels_before_frame_observers(canvas, tape, qapp):
    clock = PresentationClock(canvas)
    canvas.set_presentation_clock(clock)
    tape.set_presentation_clock(clock)
    canvas.set_interaction_priority(True)
    tape.set_interaction_priority(True)
    result = raster(canvas)
    canvas._adopt_raster(result)
    tape.set_order_flow_snapshot(snapshot(1))
    tape._refresh_table()
    observed = []
    clock.frame.connect(lambda _: observed.append(
        (canvas._display_frame is result, tape.model.rowCount())))
    settle(qapp)
    assert observed == [(True, 1)]
    assert canvas._process_link.acks == [('test-image', 0)]
    clock.cancel()


@pytest.mark.parametrize('mode', ['base', 'quote'])
def test_retained_amounts_and_outcomes_do_not_repeat_numeric_formatting(qapp, monkeypatch, mode):
    model = ui._TradesTapeModel()
    model.value_mode = mode
    prints = [replace(print_(sequence), quantity=.0125, notional=.00125)
              for sequence in range(500, 0, -1)]
    model.set_trades(prints)
    cells = [row[1] for row in model.rows]
    notifications = []
    model.dataChanged.connect(lambda first, last, *_: notifications.append(
        (first.row(), first.column(), last.column())))
    calls = []
    original = ui.format_book_price
    monkeypatch.setattr(ui, 'format_book_price', lambda *args, **kwargs:
                        calls.append(args) or original(*args, **kwargs))
    model.set_trades(prints)
    assert calls == [] and notifications == []
    prints[4] = replace(prints[4], outcome='FOLLOW_THROUGH', outcome_direction=1)
    model.set_trades(prints)
    assert calls == []
    assert notifications == [(4, 2, 2)]
    assert model.rows[4][1] == (cells[4][0], cells[4][1], '✓', cells[4][3])
    assert model.rows[4][0] == prints[4]


@pytest.mark.parametrize('mode,initial_amount,new_amount,expected', [
    ('base', 1.25, .00125, ('0.00125', '1.25000')),
    ('quote', 1.25, .00125, ('$0.00125', '$1.25000')),
])
def test_new_precision_reformats_retained_rows_and_survives_tail_removal(
        qapp, mode, initial_amount, new_amount, expected):
    model = ui._TradesTapeModel()
    model.value_mode = mode
    field = 'quantity' if mode == 'base' else 'notional'
    older = replace(print_(1), **{field: initial_amount})
    newer = replace(print_(2), **{field: new_amount})
    model.set_trades([older])
    model.set_trades([newer, older])
    assert tuple(row[1][1] for row in model.rows) == expected
    model.set_trades([older])
    assert model.rows[0][1][1] == expected[1]


@pytest.mark.parametrize('mode', ['base', 'quote'])
def test_retained_amount_correction_updates_precision_and_all_numeric_cells(qapp, mode):
    model = ui._TradesTapeModel()
    model.value_mode = mode
    original = print_(1)
    model.set_trades([original])
    field = 'quantity' if mode == 'base' else 'notional'
    corrected = replace(original, **{field: .000125})
    model.set_trades([corrected])
    prefix = '' if mode == 'base' else '$'
    assert model.rows[0][1][1] == prefix + '0.000125'
    assert model.rows[0][0] == corrected
    corrected = replace(corrected, event_time_ms=corrected.event_time_ms + 1000,
                        aggressor_side='SELL')
    changes = []
    old_time = model.rows[0][1][3]
    model.dataChanged.connect(lambda first, last, *_: changes.append(
        (first.column(), last.column())))
    model.set_trades([corrected])
    assert model.rows[0][1][3] != old_time
    assert changes == [(0, 3)]
    assert model.index(0, 0).data(QtCore.Qt.ItemDataRole.ForegroundRole) == model.sell


def test_worker_coalesces_to_configured_display_period_during_interaction(qapp, monkeypatch):
    # Control time and pump the actual worker; don't depend on host timer speed.
    now = [10.0]
    monkeypatch.setattr(ui.time, 'perf_counter', lambda: now[0])
    worker = ui._DomRasterProcess({'theme': {}, 'dpi': 96})
    # Pump no wall-clock events here: the test advances the due timer explicitly.
    worker.application = SimpleNamespace(processEvents=lambda: None)
    canvas = worker.canvas
    config = dict(symbol='BTCUSDT', market_epoch=1, typography=None, theme={},
                  tick=.1, aggregation=1, density='normal', values='quote',
                  depth=False, columns=dict(canvas.DEFAULT_COLUMN_PREFERENCES),
                  widths={}, interval=7, interaction_priority=True,
                  size=(360, 480), dpr=1.0)
    try:
        worker._configure((1, config))
        canvas._dirty_pixels = QtGui.QRegion()
        canvas._last_snapshot_prepare_at = now[0]
        assert canvas._row_frame_interval_ms() == 7  # Child screen is offscreen.
        worker.step({'snapshot': (1, snapshot(1))}, None)
        assert canvas._pending_source_snapshot.sequence == 1
        assert canvas.snapshot is None
        worker.step({'snapshot': (1, snapshot(2))}, None)
        assert canvas._pending_source_snapshot.sequence == 2
        assert canvas.snapshot is None
        assert canvas._snapshot_prepare_timer.isActive()
        # Simulate the timer's due point; only the latest derived rows prepare.
        now[0] += .007
        canvas._snapshot_prepare_timer.stop()
        canvas._flush_pending_snapshot()
        assert canvas.snapshot.sequence == 2
        assert canvas._pending_source_snapshot is None
        # Ending interaction retains immediate ordinary snapshot presentation.
        config = dict(config, interaction_priority=False)
        worker.step({'config': (2, config), 'snapshot': (1, snapshot(3))}, None)
        assert canvas.snapshot.sequence == 3
        assert canvas._pending_source_snapshot is None
    finally:
        worker.close()
