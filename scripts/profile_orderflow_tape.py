"""Offline accepted-trade capture and actual spawned-worker tape session.

Profiles burst retention, hidden views, independent modes/units, scrolling,
stalled presentation, view reseeding, resets and relay recovery. No network
connection, gateway, order or test suite is created.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import platform
import sys
import time

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
if os.environ.get('NIGHTWATCH_QT_SITE_PACKAGES'):
    sys.path.append(os.environ['NIGHTWATCH_QT_SITE_PACKAGES'])
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from PySide6 import QtCore, QtWidgets
from nightwatch.orderbook.backend import OrderFlowAnalyzer, OrderFlowRuntime
from nightwatch.orderbook.orderbook_ui import TradesTapeWidget
from nightwatch.orderbook.tape import (
    TapePublisher, TapeSeedRequired, TapeStateDecoder, TapeStateEncoder,
    TradeTapeHistory, apply_patch,
)
from nightwatch.utilities import load_app_fonts


def raw(sequence, symbol='BTCUSDT', quantity='.0001'):
    return dict(s=symbol, a=sequence, T=1700000000000 + sequence,
                p='10000.1', q=quantity, m=False)


def ticker(sequence, bid='10000', ask='10000.1'):
    return dict(s='BTCUSDT', u=sequence, b=bid, a=ask, B='2', A='2')


def capture_profile():
    analyzer = OrderFlowAnalyzer('BTCUSDT', tick_size=.1)
    analyzer.add_book_ticker(ticker(1), now=100)
    started = time.perf_counter()
    analyzer.add_trade_batch([raw(1, quantity='10')] + [raw(i) for i in range(2, 2202)], now=100)
    capture_ms = (time.perf_counter() - started) * 1000
    state = analyzer.tape_history.state()
    recent = analyzer.snapshot(now=100).recent_prints
    retained_early_large = any(record.sequence == 1 for _key, record in state.large_entries)
    analyzer.add_book_ticker(ticker(2, '10002', '10002.1'), now=100.6)
    analyzer.snapshot(now=100.6)
    corrected = next(record for _key, record in analyzer.tape_history.state().large_entries if record.sequence == 1)
    expired = analyzer.snapshot(now=110)
    revision = analyzer.tape_history.revision
    analyzer.add_trade_batch([raw(2201), raw(2202, quantity='nan'), raw(2203, quantity='-1')], now=110)
    rejected_inputs_preserved_revision = analyzer.tape_history.revision == revision
    history = analyzer.tape_history
    publisher = TapePublisher()
    publisher.set_view('all', 1, 'BTCUSDT', 'ALL', True)
    publisher.set_view('large', 1, 'BTCUSDT', 'LARGE', True)
    first = publisher.publish(history.state(), 200)
    old_state = history.state()
    encoder, decoder = TapeStateEncoder(), TapeStateDecoder()
    decoder.decode(encoder.encode(old_state))
    for sequence in range(2202, 2704):
        history.add(replace(old_state.all_entries[0][1], sequence=sequence,
                            received_monotonic=111 + sequence / 100000))
    history.resolve(1, 'REJECTED', -1)
    withheld = publisher.publish(history.state(), 201)
    for frame in first:
        publisher.ack(frame.consumer, frame.token, frame.revision)
    cumulative = publisher.publish(history.state(), 202)
    cumulative_preserved = all(
        apply_patch(old_state.all_entries if frame.mode == 'ALL' else old_state.large_entries,
                    frame.patch, seed=False)
        == (history.state().all_entries if frame.mode == 'ALL' else history.state().large_entries)
        for frame in cumulative)
    delta = encoder.encode(history.state())
    lost_decoder = TapeStateDecoder()
    missing_base_detected = False
    try:
        lost_decoder.decode(delta)
    except TapeSeedRequired:
        missing_base_detected = True
    restored = lost_decoder.decode(encoder.encode(history.state(), seed=True))
    checkpoint_preserved = restored == history.state()
    for frame in cumulative:
        publisher.ack(frame.consumer, frame.token, frame.revision)
    history.set_threshold(history.threshold + 1)
    metadata = publisher.publish(history.state(), 203)
    before_reset = history.state()
    history.reset('BTCUSDT')
    history.add(replace(before_reset.all_entries[0][1], sequence=1, received_monotonic=112))
    reset_keys_unique = len(set(key for key, _record in history.state().all_entries)) == 500
    publisher.set_view('all', 2, 'BTCUSDT', 'ALL', False)
    publisher.set_view('large', 2, 'BTCUSDT', 'LARGE', False)
    hidden_frames = publisher.publish(history.state(), 203)
    history.reset('ETHUSDT')
    return dict(accepted_burst=2201, capture_ms=round(capture_ms, 4),
                recent_snapshot_rows=len(recent), recent_rows_after_expiry=len(expired.recent_prints),
                retained_history_rows=[len(old_state.all_entries), len(old_state.large_entries)],
                early_large_outside_recent_window=retained_early_large and all(p.sequence != 1 for p in recent),
                evicted_print_outcome_corrected=corrected.outcome == 'FOLLOW_THROUGH' and corrected.outcome_direction == 1,
                duplicate_and_invalid_inputs_ignored=rejected_inputs_preserved_revision,
                stalled_views_emit_no_additional_frames=not withheld,
                cumulative_frames_preserve_all_records=cumulative_preserved and len(cumulative) == 2,
                missing_checkpoint_base_detected=missing_base_detected,
                checkpoint_seed_preserves_all_records=checkpoint_preserved,
                threshold_changes_update_only_large_view=[frame.consumer for frame in metadata] == ['large'] and not metadata[0].patch.upserts,
                same_symbol_reset_keys_unique=reset_keys_unique,
                hidden_view_frames=len(hidden_frames),
                changed_symbol_history_empty=not history.state().all_entries and not history.state().large_entries)


class _Driver(QtCore.QObject):
    command = QtCore.Signal(str, object)

    @QtCore.Slot()
    def finish(self):
        QtCore.QCoreApplication.instance().quit()


class _RelayControl(QtCore.QObject):
    stopped = QtCore.Signal()

    def __init__(self, runtime):
        super().__init__(runtime)
        self.runtime = runtime

    @QtCore.Slot(str, object)
    def command(self, name, args):
        if name == 'stop':
            self.runtime.shutdown()
            self.stopped.emit()
        else:
            getattr(self.runtime, name)(*args)


class _HeldClock(QtCore.QObject):
    interaction_frame = QtCore.Signal(float)

    def request(self):
        pass


def session_profile(app):
    tapes = [TradesTapeWidget({}), TradesTapeWidget({})]
    runtime = OrderFlowRuntime('BTCUSDT', tick_size=.1, quote_volume=1e8)
    relay_thread = QtCore.QThread()
    relay_thread.setObjectName('offline-order-flow-relay')
    control = _RelayControl(runtime)
    runtime.moveToThread(relay_thread)
    driver = _Driver()
    driver.command.connect(control.command, QtCore.Qt.ConnectionType.QueuedConnection)
    relay_thread.start()
    errors, expected_interruptions, links = [], [], [runtime._link]
    observations = {}
    generation, sequence = 7, 2201
    phase, phase_started = 0, time.monotonic()
    started = phase_started
    held_clock = _HeldClock()
    finished = False

    def call(name, *args):
        driver.command.emit(name, args)

    def failed(_generation, message):
        (expected_interruptions if message == 'offline relay recovery profile' else errors).append(message)

    runtime.failed.connect(failed, QtCore.Qt.ConnectionType.QueuedConnection)
    for index, tape in enumerate(tapes):
        tape.resize(450, 600)
        tape.set_tape_source(runtime.tape_source)
        tape.set_mode('ALL' if index == 0 else 'LARGE')
        tape.set_panel_active(True)
        tape.show()
    call('reset_model', generation, 'BTCUSDT', .1, 1e8)
    call('set_active', generation, True)
    call('add_book_ticker', generation, ticker(1))
    call('add_trade_batch', generation, [raw(1, quantity='10')] + [raw(i) for i in range(2, sequence + 1)])

    def entries(tape):
        return tuple(zip(tape.model.keys, (record for record, _cells in tape.model.rows)))

    def matches(state):
        return state is not None and all(entries(tape) == (state.all_entries if tape.mode() == 'ALL' else state.large_entries)
                                         for tape in tapes)

    def advance():
        nonlocal phase, phase_started
        phase += 1
        phase_started = time.monotonic()
        print(f'Tape profile phase {phase}', file=sys.stderr, flush=True)

    def batch(count, *, symbol='BTCUSDT', first=None):
        nonlocal sequence
        if first is not None:
            sequence = first - 1
        records = [raw(i, symbol) for i in range(sequence + 1, sequence + count + 1)]
        sequence += count
        call('add_trade_batch', generation, records)

    def finish():
        nonlocal finished
        if finished:
            return
        finished = True
        timer.stop()
        observations['completed_phases'] = phase
        observations['duration_seconds'] = round(time.monotonic() - started, 3)
        observations['gui_models_and_source_affinity'] = runtime.tape_source.thread() == app.thread() and all(
            tape.model.thread() == app.thread() for tape in tapes)
        observations['source_delivered_frames'] = runtime.tape_source.delivered_frames
        observations['source_upsert_records'] = runtime.tape_source.applied_records
        observations['model_formatted_rows'] = [tape.model.formatted_rows for tape in tapes]
        observations['expected_relay_interruptions'] = expected_interruptions
        observations['errors'] = errors
        print(json.dumps(observations), file=sys.stderr, flush=True)
        if runtime._link not in links:
            links.append(runtime._link)
        call('stop')

    control.stopped.connect(driver.finish, QtCore.Qt.ConnectionType.QueuedConnection)
    timer = QtCore.QTimer()
    timer.setInterval(20)
    scratch = {}

    def tick():
        nonlocal generation
        if time.monotonic() - phase_started > 6:
            errors.append(f'Phase {phase} did not converge to its worker checkpoint')
            finish()
            return
        state = runtime._link.tape_checkpoint()
        if phase == 0 and state and state.all_entries and state.all_entries[0][1].sequence == sequence and matches(state):
            observations['burst_views_match_canonical_records'] = True
            observations['early_large_present'] = any(record.sequence == 1 for _key, record in state.large_entries)
            for tape in tapes:
                tape.hide()
            scratch['hidden_counters'] = [(tape._print_cache_misses, tape.model.formatted_rows) for tape in tapes]
            batch(501)
            advance()
        elif phase == 1 and state and state.all_entries[0][1].sequence == sequence and time.monotonic() - phase_started > .35:
            observations['hidden_models_receive_zero_updates'] = scratch['hidden_counters'] == [
                (tape._print_cache_misses, tape.model.formatted_rows) for tape in tapes]
            for index, tape in enumerate(tapes):
                tape.set_mode('LARGE' if index == 0 else 'ALL')
                tape.show()
            advance()
        elif phase == 2 and matches(state):
            observations['show_and_independent_modes_match_canonical_records'] = True
            tape = tapes[1]
            tape.table.scrollTo(tape.model.index(120, 0), QtWidgets.QAbstractItemView.ScrollHint.PositionAtTop)
            top = tape.table.rowAt(0)
            scratch['anchor'] = tape.model.keys[top], tape.table.rowViewportPosition(top)
            batch(3)
            advance()
        elif phase == 3 and state and state.all_entries[0][1].sequence == sequence and matches(state):
            tape = tapes[1]
            top = tape.table.rowAt(0)
            observations['scroll_anchor_preserved'] = scratch['anchor'] == (tape.model.keys[top], tape.table.rowViewportPosition(top))
            tapes[0].set_mode('ALL')
            tapes[0].set_value_mode('base')
            tapes[1].set_mode('LARGE')
            advance()
        elif phase == 4 and matches(state) and tapes[0].model.value_mode == 'base':
            observations['base_quantity_precision_preserved'] = tapes[0].model.rows[0][1][1] == '0.0001'
            tapes[0].set_presentation_clock(held_clock)
            tapes[0].set_interaction_priority(True)
            batch(5)
            advance()
        elif phase == 5 and tapes[0]._pending_tape_frame is not None:
            scratch['held_revision'] = tapes[0]._pending_tape_frame.revision
            batch(501)
            advance()
        elif phase == 6 and state and state.all_entries[0][1].sequence == sequence:
            observations['held_gui_does_not_block_worker_history'] = tapes[0]._pending_tape_frame.revision == scratch['held_revision']
            tapes[0].set_interaction_priority(False)
            advance()
        elif phase == 7 and matches(state):
            observations['held_gui_cumulative_delta_preserves_records'] = True
            scratch['old_token'] = tapes[0]._tape_token
            tapes[0]._tape_revision = -1  # Profile the missing-view-base seed path.
            batch(2)
            advance()
        elif phase == 8 and state and state.all_entries[0][1].sequence == sequence and matches(state):
            observations['missing_view_base_reseeded'] = tapes[0]._tape_token > scratch['old_token']
            scratch['same_symbol_old'] = state
            generation += 1
            call('reset_model', generation, 'BTCUSDT', .1, 1e8)
            batch(1, first=1)
            advance()
        elif phase == 9 and state and state.epoch > scratch['same_symbol_old'].epoch and matches(state):
            observations['same_symbol_reset_preserves_history'] = state.all_entries[1:] == scratch['same_symbol_old'].all_entries[:-1]
            observations['same_symbol_reset_has_unique_keys'] = len(set(tapes[0].model.keys)) == 500
            scratch['pre_restart'] = state
            call('_failed', 'offline relay recovery profile')
            advance()
        elif phase == 10 and state and state.epoch > scratch['pre_restart'].epoch and matches(state):
            observations['relay_restart_restores_received_history'] = state.all_entries == scratch['pre_restart'].all_entries and state.large_entries == scratch['pre_restart'].large_entries
            if runtime._link not in links:
                links.append(runtime._link)
            batch(1, first=1)
            advance()
        elif phase == 11 and state and state.all_entries[0][0][0] > scratch['pre_restart'].epoch and matches(state):
            observations['restart_sequences_do_not_collide'] = len(set(tapes[0].model.keys)) == 500
            generation += 1
            call('reset_model', generation, 'ETHUSDT', .1, 1e8)
            batch(10, symbol='ETHUSDT', first=1)
            advance()
        elif phase == 12 and state and state.symbol == 'ETHUSDT' and len(state.all_entries) == 10:
            for tape in tapes:
                tape.set_market('ETHUSDT', tick_size=.1)
            advance()
        elif phase == 13 and matches(state):
            observations['delayed_market_view_does_not_clear_accepted_trades'] = len(state.all_entries) == 10
            observations['changed_symbol_has_no_previous_market_records'] = all(record.sequence <= 10 for _key, record in state.all_entries)
            observations['final_history_rows'] = [len(state.all_entries), len(state.large_entries)]
            advance()
            finish()

    timer.timeout.connect(tick)
    timer.start()
    app.exec()
    relay_thread.quit()
    observations['relay_thread_stopped'] = relay_thread.wait(2000)
    for link in links:
        if link._thread is not None:
            link._thread.join(timeout=2)
    for tape in tapes:
        tape.close()
    app.processEvents()
    return observations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setQuitOnLastWindowClosed(False)
    load_app_fonts(app, str(SOURCE / 'nightwatch'))
    report = dict(python=platform.python_version(), qt=QtCore.qVersion(), platform=platform.platform(),
                  measurement='offline accepted-trade capture and actual spawned analysis process, QThread relay and offscreen Qt tables; no desktop-FPS claim',
                  capture=capture_profile(), session=session_profile(app))
    output = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.write_text(output)
    print(output, end='')


if __name__ == '__main__':
    main()
