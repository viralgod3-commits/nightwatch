"""Offline Qt analysis/raster session with synthetic ordered market inputs.

Exercises regional adoption, interaction pacing, hidden frames, resize, market
reset, aggregation and two tape consumers. No exchange connection is created.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import sys

SOURCE = Path(os.environ.get('NIGHTWATCH_BENCH_SOURCE', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(SOURCE))
if os.environ.get('NIGHTWATCH_QT_SITE_PACKAGES'):
    sys.path.append(os.environ['NIGHTWATCH_QT_SITE_PACKAGES'])
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from PySide6 import QtCore, QtWidgets
from nightwatch.orderbook.backend import OrderFlowRuntime
from nightwatch.orderbook.orderbook_ui import OrderFlowDomCanvas, TradesTapeWidget
from nightwatch.presentation import PresentationClock
from nightwatch.utilities import load_app_fonts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setQuitOnLastWindowClosed(False)
    load_app_fonts(app, str(SOURCE / 'nightwatch'))
    canvas = OrderFlowDomCanvas({})
    canvas.resize(803, 907)
    canvas.set_symbol('BTCUSDT')
    canvas.set_price_tick_size(.1)
    canvas.set_aggregation_multiplier(5)
    clock = PresentationClock(canvas)
    canvas.set_presentation_clock(clock)
    canvas.set_interaction_priority(True)
    canvas.show()
    occluder = QtWidgets.QWidget(canvas)
    occluder.setGeometry(50, 220, 650, 150)
    occluder.setAutoFillBackground(True)
    occluder.setAttribute(QtCore.Qt.WidgetAttribute.WA_OpaquePaintEvent)
    occluder.hide()
    tapes = [TradesTapeWidget({}), TradesTapeWidget({})]
    runtime = OrderFlowRuntime('BTCUSDT', tick_size=.1, quote_volume=1e8,
                               min_snapshot_interval_ms=7, interaction_snapshot_interval_ms=16)
    errors = []
    runtime.failed.connect(lambda generation, message: errors.append(message))
    canvas._process_link.failed.connect(errors.append)
    snapshots = trades = tick = 0
    generation = 7
    sessions = set()
    latest_source_sequence = -1
    timings = []
    rendered = []
    changes = []

    def received(epoch, payload):
        nonlocal snapshots, latest_source_sequence
        frame = payload[0] if isinstance(payload, tuple) else payload
        snapshot = frame.snapshot if hasattr(frame, 'snapshot') else frame
        snapshots += 1
        latest_source_sequence = snapshot.sequence
        if snapshot.component_revisions:
            sessions.add(snapshot.component_revisions.stream)
        canvas.set_snapshot(payload)
        for tape in tapes:
            tape.set_order_flow_snapshot(snapshot)

    def raster_received(result):
        if result.get('frame') is not None:
            rendered.append((result['market_epoch'], result.get('frame_revision'),
                             len(result.get('dirty_rects', ()))))

    runtime.snapshot_ready.connect(received)
    canvas._process_link.ready.connect(raster_received)

    def seed():
        runtime.reset_model(generation, 'BTCUSDT', .1, 1e8)
        runtime.set_depth_capacity(generation, 120)
        runtime.set_active(generation, True)
        runtime.set_interaction_priority(generation, True)

    def depth():
        bids = [(10000 - i * .1, 2 + ((tick // 8) % 2 if i == 0 else 0)) for i in range(120)]
        asks = [(10000.1 + i * .1, 2) for i in range(120)]
        runtime.add_depth(generation, bids, asks, tick + 1)

    seed()
    depth()
    timer = QtCore.QTimer()
    timer.setInterval(20)

    def advance():
        nonlocal tick, trades, generation
        tick += 1
        if tick == 50:
            canvas.resize(811, 919)
            changes.append('resize')
        elif tick == 70:
            canvas.hide()
            changes.append('hide')
        elif tick == 80:
            canvas.show()
            changes.append('show')
        elif tick == 100:
            canvas.reset()
            generation += 1
            seed()
            depth()
            changes.append('same-symbol market reset')
        elif tick == 130:
            canvas.set_aggregation_multiplier(1)
            changes.append('aggregation')
        elif tick == 160:
            canvas.set_value_mode('base')
            canvas.set_interaction_priority(False)
            changes.append('units and interaction pacing')
        elif tick == 170:
            occluder.show()
            changes.append('partially occluded paints')
        elif tick == 180:
            occluder.hide()
            changes.append('exposure after occlusion')
        shift = .1 * (tick % 2)
        runtime.add_book_ticker(generation, dict(s='BTCUSDT', u=tick + 1,
                                                b=str(10000 + shift), a=str(10000.1 + shift), B='2', A='2'))
        if tick % 8 == 0:
            depth()
        if tick % 5 == 0:
            trades += 1
            runtime.add_trade_batch(generation, [dict(s='BTCUSDT', t=tick, T=1700000000000 + tick * 20,
                                                       p='10000.1', q='2', m=False)])
        if tick % 4 == 0:
            canvas._send('pointer', (400.0, 240.0 + tick % 100))
        timings.append(canvas.last_paint_ms)
        if tick == 200:
            timer.stop()
            QtCore.QTimer.singleShot(700, finish)

    report = {}

    def finish():
        nonlocal report
        timer.stop()
        stats = canvas.performance_state()
        frame = canvas._display_frame
        report = dict(python=platform.python_version(), qt=QtCore.qVersion(),
                      platform=platform.platform(), dpr=canvas.devicePixelRatioF(),
                      measurement='actual spawned analysis/raster workers and offscreen QWidget; no desktop/display-FPS claim',
                      input_ticks=tick, trade_events=trades, source_snapshots=snapshots,
                      source_sessions=len(sessions), latest_source_sequence=latest_source_sequence,
                      latest_displayed_sequence=frame['sequence'] if frame else None,
                      raster_frames=len(rendered), max_transported_rects=max((r[2] for r in rendered), default=0),
                      retained_tape_rows=[len(tape._history) for tape in tapes],
                      changes=changes, errors=errors, renderer_error=canvas._remote_error,
                      awaiting_raster_ack=canvas._raster_ack_pending,
                      gui_ms_max=round(max(timings, default=0), 4),
                      counters={name: value for name, value in stats.items()
                                if name.startswith(('gui_', 'raster_sync_'))})
        runtime.shutdown()
        canvas.close()
        for tape in tapes:
            tape.close()
        app.quit()

    timer.timeout.connect(advance)
    timer.start()
    QtCore.QTimer.singleShot(15000, finish)
    app.exec()
    for link in (runtime._link, canvas._process_link):
        if link._thread is not None:
            link._thread.join(timeout=2)
    output = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.write_text(output)
    print(output, end='')


if __name__ == '__main__':
    main()
