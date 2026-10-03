"""Offline cache-work measurements through both incremental snapshot codecs.

NIGHTWATCH_BENCH_SOURCE selects an earlier checkout for the same harness.
Rendering, kernel pipes and actual display FPS are outside these measurements.
No exchange connection, gateway or live order is created.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
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
from nightwatch.models import (
    OrderFlowDisplayLevel, OrderFlowPresentationFrame, OrderFlowSnapshot,
    OrderFlowTradePrint,
)
from nightwatch.orderbook.ipc import SnapshotDecoder, SnapshotEncoder, install_snapshot_reducers
from nightwatch.orderbook.orderbook_ui import (
    TradesTapeWidget, _DomRasterWorkerCanvas, aggregate_order_flow_snapshot,
)
from nightwatch.utilities import load_app_fonts

try:
    from nightwatch.orderbook.revisions import OrderFlowRevisionTracker
except ModuleNotFoundError as error:
    if error.name != 'nightwatch.orderbook.revisions':
        raise
    OrderFlowRevisionTracker = None


def summary(samples):
    ordered = sorted(samples)
    return dict(median_ms=round(statistics.median(samples), 4),
                p95_ms=round(ordered[round((len(ordered) - 1) * .95)], 4),
                p99_ms=round(ordered[round((len(ordered) - 1) * .99)], 4))


def initial_snapshot(levels, prints):
    now = time.monotonic()

    def level(side, index):
        price = 10000 + (.1 + index * .1 if side == 'ask' else -index * .1)
        return OrderFlowDisplayLevel(
            side=side, price=price, quantity=2, notional=price * 2,
            delta_notional_5s=100, trade_notional_5s=300,
            signed_trade_notional_5s=100, rpi_trade_notional_5s=0,
            age_seconds=2, persistence_ratio=.8, replenishments=2,
            state='RESTING', liquidity_intensity=.5, delta_intensity=.2,
            trade_intensity=.3, liquidity_history_30s=(.2,) * 8,
        )

    return OrderFlowSnapshot(
        symbol='BTCUSDT', sequence=1, generated_monotonic=now,
        ready=True, live=True, bbo_source='bookTicker', depth_age_seconds=.01,
        bbo_age_seconds=.001, trade_age_seconds=.01, best_bid=10000,
        best_ask=10000.1, midpoint=10000.05, large_trade_threshold=100,
        bid_levels=tuple(level('bid', i) for i in range(levels)),
        ask_levels=tuple(level('ask', i) for i in range(levels)),
        recent_prints=tuple(OrderFlowTradePrint(
            sequence=i + 1, event_time_ms=1700000000000 + i,
            received_monotonic=now - .05 + i / 100000, price=10000,
            quantity=.01, notional=100, aggressor_side='buy',
            normal_notional=100, rpi_notional=0, relative_size=1, salience_class=1,
        ) for i in range(prints)),
    )


def advance(snapshot, case, iteration):
    changes = dict(sequence=snapshot.sequence + 1,
                   generated_monotonic=time.monotonic(),
                   best_bid=10000 + (iteration % 2) * .1,
                   best_ask=10000.1 + (iteration % 2) * .1)
    if case == 'age_refresh':
        for side in ('bid_levels', 'ask_levels'):
            changes[side] = tuple(replace(row, age_seconds=row.age_seconds + .1,
                                         analysis_revision=row.analysis_revision + 1)
                                  for row in getattr(snapshot, side))
    elif case == 'amount_corrections':
        changes['bid_levels'] = tuple(
            replace(row, quantity=row.quantity + .01, notional=row.notional + row.price * .01)
            if i < 5 else row for i, row in enumerate(snapshot.bid_levels))
    elif case == 'far_amount_corrections':
        changes['bid_levels'] = tuple(
            replace(row, quantity=row.quantity + .01, notional=row.notional + row.price * .01)
            if i == len(snapshot.bid_levels) - 1 else row
            for i, row in enumerate(snapshot.bid_levels))
    elif case == 'signed_flow':
        changes['bid_levels'] = tuple(
            replace(row, signed_trade_notional_5s=row.signed_trade_notional_5s + 100)
            if i < 5 else row for i, row in enumerate(snapshot.bid_levels))
    elif case == 'outcome_corrections':
        end = len(snapshot.recent_prints) - (iteration - 1) * 10
        changes['recent_prints'] = tuple(
            replace(trade, outcome='FOLLOW_THROUGH', outcome_direction=1)
            if end - 10 <= i < end else trade for i, trade in enumerate(snapshot.recent_prints))
    return replace(snapshot, **changes)


def roundtrip(value):
    return pickle.loads(ForkingPickler.dumps(value, 5))


def measure(app, case, levels, prints, samples):
    canvas = _DomRasterWorkerCanvas({})
    canvas.resize(800, 900)
    canvas.set_price_tick_size(.1)
    canvas.set_aggregation_multiplier(5)
    tapes = [TradesTapeWidget({}), TradesTapeWidget({})]
    snapshot = initial_snapshot(levels, prints)
    tracker = OrderFlowRevisionTracker() if OrderFlowRevisionTracker else None
    analysis_sender, app_receiver = SnapshotEncoder(), SnapshotDecoder()
    dom_sender, dom_receiver = SnapshotEncoder(), SnapshotDecoder()
    values = {name: [] for name in ('producer_revision', 'tapes', 'aggregation',
                                    'amount_width', 'raster_prepare', 'cache_work')}
    current = {}
    formatting_calls = 0
    measuring_width = False
    original_amount = canvas._row_amount
    original_width = canvas._required_amount_lane_width
    original_prepare = canvas._aggregation_worker.prepare

    def amount(*args, **kwargs):
        nonlocal formatting_calls
        if measuring_width:
            formatting_calls += 1
        return original_amount(*args, **kwargs)

    def width(*args, **kwargs):
        nonlocal measuring_width
        measuring_width = True
        started = time.perf_counter()
        try:
            return original_width(*args, **kwargs)
        finally:
            current['amount_width'] += (time.perf_counter() - started) * 1000
            measuring_width = False

    def prepare(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original_prepare(*args, **kwargs)
        finally:
            current['aggregation'] += (time.perf_counter() - started) * 1000

    canvas._row_amount = amount
    canvas._required_amount_lane_width = width
    canvas._aggregation_worker.prepare = prepare
    preserved = True
    try:
        for iteration in range(samples + 1):
            if iteration:
                snapshot = advance(snapshot, case, iteration)
            current = {name: 0.0 for name in values}
            started = time.perf_counter()
            if tracker:
                revisions = tracker.update(snapshot.bid_levels, snapshot.ask_levels, snapshot.recent_prints)
            current['producer_revision'] = (time.perf_counter() - started) * 1000
            if tracker:
                snapshot = replace(snapshot, component_revisions=revisions)
            frame = OrderFlowPresentationFrame(snapshot)
            force_seed = case == 'reseeded_bbo'
            incoming = app_receiver.decode(roundtrip(analysis_sender.encode(
                frame, 7, force_seed=force_seed)), 7)
            started = time.perf_counter()
            for tape in tapes:
                tape.set_order_flow_snapshot(incoming.snapshot)
            current['tapes'] = (time.perf_counter() - started) * 1000
            received = dom_receiver.decode(roundtrip(dom_sender.encode(
                incoming, 3, force_seed=force_seed)), 3)
            started = time.perf_counter()
            canvas._commit_full_snapshot(received.snapshot)
            current['raster_prepare'] = (time.perf_counter() - started) * 1000
            current['cache_work'] = current['tapes'] + current['raster_prepare']
            if iteration:
                for name in values:
                    values[name].append(current[name])
            # Outside measured intervals, compare all display fields against
            # uncached aggregation; this also detects stale signed-flow reuse.
            fresh = aggregate_order_flow_snapshot(received.snapshot, 5, .1)
            preserved &= asdict(canvas.snapshot) == asdict(fresh)
        return dict(
            case=case, samples=samples, complete_display_fields_preserved=preserved,
            timings={name: summary(data) for name, data in values.items()},
            amount_width_formatting_calls_including_seed=formatting_calls,
            aggregation_revision_reuses=getattr(canvas._aggregation_worker, 'reused', None),
            amount_width_cache_hits=getattr(canvas, '_amount_width_cache_hits', None),
            amount_width_cache_misses=getattr(canvas, '_amount_width_cache_misses', None),
            tape_print_cache_hits=[getattr(tape, '_print_cache_hits', None) for tape in tapes],
            retained_tape_rows=[len(tape._history) for tape in tapes],
        )
    finally:
        canvas._aggregation_job.close()
        canvas.close()
        for tape in tapes:
            tape.close()
        app.processEvents()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--levels', type=int, default=120)
    parser.add_argument('--prints', type=int, default=2048)
    parser.add_argument('--samples', type=int, default=30)
    parser.add_argument('--label', default='working-tree')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not (0 <= args.levels <= 1000 and 0 <= args.prints <= 2048 and args.samples >= 1):
        parser.error('Use bounded collections and a positive sample count')
    install_snapshot_reducers()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    load_app_fonts(app, str(SOURCE / 'nightwatch'))
    report = dict(
        label=args.label, python=platform.python_version(), qt=QtCore.qVersion(),
        platform=platform.platform(), levels_per_side=args.levels, retained_prints=args.prints,
        measurement='cache work after two incremental codecs; excludes serialization, pipes, paint and FPS',
        revision_metadata=OrderFlowRevisionTracker is not None,
        cases=[measure(app, case, args.levels, args.prints, args.samples) for case in (
            'bbo_only', 'age_refresh', 'amount_corrections', 'far_amount_corrections', 'outcome_corrections',
            'reseeded_bbo', 'signed_flow')],
    )
    text = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.write_text(text)
    print(text, end='')


if __name__ == '__main__':
    main()
