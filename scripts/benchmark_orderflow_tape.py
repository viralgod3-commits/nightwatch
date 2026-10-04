"""Offline tape ingestion/model costs for the same synthetic input sequence.

NIGHTWATCH_BENCH_SOURCE selects the pre-change checkout. No live exchange,
order, desktop compositor or test suite is involved.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from multiprocessing.reduction import ForkingPickler
import os
from pathlib import Path
import pickle
import platform
import statistics
import sys
import time

SOURCE = Path(os.environ.get('NIGHTWATCH_BENCH_SOURCE', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(SOURCE))
if os.environ.get('NIGHTWATCH_QT_SITE_PACKAGES'):
    sys.path.append(os.environ['NIGHTWATCH_QT_SITE_PACKAGES'])
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from PySide6 import QtCore, QtWidgets
from nightwatch.orderbook.ipc import install_snapshot_reducers
from nightwatch.orderbook.orderbook_ui import TradesTapeWidget
from nightwatch.orderbook.revisions import OrderFlowRevisionTracker
from nightwatch.models import OrderFlowSnapshot, OrderFlowTradePrint

try:
    from nightwatch.orderbook.tape import TapePublisher, TapeStateEncoder, TradeTapeHistory
except ModuleNotFoundError as error:
    if error.name != 'nightwatch.orderbook.tape':
        raise
    TradeTapeHistory = None


class _MeasuredSource:
    """Only the subscription/ack boundary; history runs outside GUI intervals."""

    def __init__(self):
        self.commands = []
        self.consumer = 0

    def register(self, _widget):
        self.consumer += 1
        return str(self.consumer)

    def unregister(self, _consumer):
        pass

    def refresh(self, widget):
        self.command('set_tape_view', (widget._tape_consumer, widget._tape_token,
                                      widget.symbol, widget.mode(),
                                      widget._active and widget.isVisible()))

    def command(self, name, args):
        self.commands.append((name, args))

    def ingest_snapshot(self, _snapshot):
        pass


def timings(samples):
    ordered = sorted(samples)
    return dict(total_ms=round(sum(samples), 4), median_ms=round(statistics.median(samples), 4),
                p95_ms=round(ordered[round((len(ordered) - 1) * .95)], 4),
                max_ms=round(max(samples), 4))


def trade(sequence):
    return OrderFlowTradePrint(sequence, 1700000000000 + sequence,
                              100 + sequence / 100000, 10000, .01, 100,
                              'buy', 100, 0, 2, 1)


def measure(app, case, frames):
    visible = case != 'hidden_burst'
    tapes = [TradesTapeWidget({}), TradesTapeWidget({})]
    source = _MeasuredSource() if TradeTapeHistory else None
    formatted = [0, 0]
    for index, tape in enumerate(tapes):
        tape.set_mode('ALL' if index == 0 else 'LARGE')
        tape.resize(450, 600)
        original = tape.model._row

        def row(record, original=original, index=index):
            formatted[index] += 1
            return original(record)

        tape.model._row = row
        if source is not None:
            tape.set_tape_source(source)
        tape.set_panel_active(visible)
        if visible:
            tape.show()
    app.processEvents()
    prints = tuple(trade(i) for i in range(1, 2049))
    sequence = 2048
    tracker = OrderFlowRevisionTracker()
    history = TradeTapeHistory('BTCUSDT') if source else None
    publisher, encoder = (TapePublisher(), TapeStateEncoder()) if source else (None, None)
    if history:
        for record in prints:
            history.add(record)

    def commands():
        if source:
            for name, args in source.commands:
                if name == 'set_tape_view':
                    publisher.set_view(*args)
                elif name == 'ack_tape':
                    publisher.ack(*args)
            source.commands.clear()

    commands()
    gui, worker, wire = [], [], []
    seed_ms = 0.0
    seeded_formatting = None
    complete_fields = True
    delivered = 0
    for frame_index in range(frames + 1):
        changed = frame_index > 0 and (case == 'hidden_burst' or frame_index % 6 == 0)
        if changed and case in ('append', 'hidden_burst'):
            incoming = tuple(trade(i) for i in range(sequence + 1, sequence + 11))
            sequence += 10
            prints = (prints + incoming)[-2048:]
        elif changed and case == 'outcome_corrections':
            cursor = ((frame_index // 6 - 1) * 10) % 500
            first, last = len(prints) - cursor - 10, len(prints) - cursor
            incoming = tuple(replace(record,
                                     outcome='FOLLOW_THROUGH' if record.outcome != 'FOLLOW_THROUGH' else 'REJECTED',
                                     outcome_direction=1 if record.outcome != 'FOLLOW_THROUGH' else -1)
                             for record in prints[first:last])
            prints = prints[:first] + incoming + prints[last:]
        else:
            incoming = ()
        snapshot = OrderFlowSnapshot(symbol='BTCUSDT', sequence=frame_index + 1,
                                     generated_monotonic=100 + frame_index / 60,
                                     ready=True, live=True, bbo_source='bookTicker',
                                     depth_age_seconds=.01, bbo_age_seconds=.01, trade_age_seconds=.01,
                                     recent_prints=prints, large_trade_threshold=1000,
                                     component_revisions=tracker.update((), (), prints))
        worker_ms = wire_bytes = 0
        if history:
            started = time.perf_counter()
            for record in incoming:
                (history.add if case != 'outcome_corrections' else history.correct)(record)
            state = history.state()
            outbound = publisher.publish(state, 100 + frame_index / 60)
            checkpoint = encoder.encode(state)
            # Includes wire encoding; receiving/decoding belongs to the relay.
            encoded = ForkingPickler.dumps((outbound, checkpoint), 5)
            worker_ms = (time.perf_counter() - started) * 1000
            wire_bytes = len(encoded)
            outbound, _checkpoint = pickle.loads(encoded)
            started = time.perf_counter()
            for update in outbound:
                tapes[int(update.consumer) - 1]._receive_tape_frame(update)
            gui_ms = (time.perf_counter() - started) * 1000 if outbound else 0.0
            delivered += len(outbound)
            commands()
        else:
            started = time.perf_counter()
            for tape in tapes:
                tape.set_order_flow_snapshot(snapshot)
                if visible and (frame_index == 0 or frame_index % 6 == 0):
                    tape._commit_table_refresh()
            gui_ms = (time.perf_counter() - started) * 1000
        if frame_index:
            gui.append(gui_ms)
            worker.append(worker_ms)
            wire.append(wire_bytes)
        else:
            seed_ms = gui_ms
            seeded_formatting = list(formatted)
        if visible and frame_index % 6 == 0:
            expected = list(reversed(prints[-500:]))
            complete_fields &= all([record for record, _cells in tape.model.rows] == expected for tape in tapes)
    result = dict(case=case, source_snapshot_frames=frames, visible_views=2 if visible else 0,
                  gui=timings(gui), worker_history_publish_and_encode=timings(worker),
                  seed_gui_ms=round(seed_ms, 4), seeded_formatted_rows=seeded_formatting,
                  subsequently_formatted_rows=[n - seed for n, seed in zip(formatted, seeded_formatting)],
                  visible_records_preserved=complete_fields if visible else None,
                  retained_model_rows=[len(tape.model.rows) for tape in tapes],
                  gui_frames_delivered=delivered if source else None,
                  worker_checkpoint_and_view_bytes=sum(wire) if source else None)
    if source:
        result['canonical_history_rows'] = [len(history.state().all_entries), len(history.state().large_entries)]
    else:
        result['independent_gui_history_rows'] = [len(tape._history) for tape in tapes]
    for tape in tapes:
        tape.close()
    app.processEvents()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frames', type=int, default=300)
    parser.add_argument('--label', default='working-tree')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.frames < 6:
        parser.error('Use at least six input frames')
    install_snapshot_reducers()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    report = dict(label=args.label, python=platform.python_version(), qt=QtCore.qVersion(),
                  platform=platform.platform(),
                  measurement='two tape ingestion/model paths, 60 source snapshots/sec and at most 10 table updates/sec; excludes paint, kernel pipes and desktop FPS',
                  results=[measure(app, case, args.frames) for case in
                           ('bbo_only', 'append', 'outcome_corrections', 'hidden_burst')])
    text = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.write_text(text)
    print(text, end='')


if __name__ == '__main__':
    main()
