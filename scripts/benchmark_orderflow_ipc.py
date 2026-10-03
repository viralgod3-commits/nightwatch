"""Offline two-hop snapshot transport measurements; no Qt or exchange access.

The old path serializes complete immutable models at both pipe boundaries. The
incremental path includes encoding, serialization, reconstruction and the second
encoding. Native pipe scheduling and onscreen FPS are outside this measurement.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import json
from multiprocessing.reduction import ForkingPickler
from pathlib import Path
import pickle
import platform
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nightwatch.models import (
    OrderFlowDisplayLevel, OrderFlowPresentationFrame, OrderFlowSnapshot,
    OrderFlowTradePrint,
)
from nightwatch.orderbook.ipc import (
    SnapshotDecoder, SnapshotEncoder, install_snapshot_reducers,
)


def exchange(value):
    packet = ForkingPickler.dumps(value, 5)
    return pickle.loads(packet), len(packet)


def initial_frame(levels, prints):
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

    def trade(sequence):
        return OrderFlowTradePrint(
            sequence=sequence, event_time_ms=1700000000000 + sequence,
            received_monotonic=1000 + sequence / 10000, price=10000,
            quantity=.01, notional=100, aggressor_side='BUY', normal_notional=100,
            rpi_notional=0, relative_size=1, salience_class=1,
        )

    snapshot = OrderFlowSnapshot(
        symbol='BTCUSDT', sequence=1, generated_monotonic=1001,
        ready=True, live=True, bbo_source='bookTicker', depth_age_seconds=.01,
        bbo_age_seconds=.001, trade_age_seconds=.01,
        bid_levels=tuple(level('bid', i) for i in range(levels)),
        ask_levels=tuple(level('ask', i) for i in range(levels)),
        recent_prints=tuple(trade(i + 1) for i in range(prints)),
        best_bid=10000, best_ask=10000.1,
    )
    return OrderFlowPresentationFrame(snapshot, 1000.999, 1001)


def advance(frame, case, iteration):
    old = frame.snapshot
    changes = dict(sequence=old.sequence + 1,
                   generated_monotonic=old.generated_monotonic + 1 / 60,
                   best_bid=10000 + iteration / 1000,
                   best_ask=10000.1 + iteration / 1000)
    if case == 'sparse_corrections':
        changes['bid_levels'] = tuple(
            replace(row, age_seconds=row.age_seconds + .1,
                    analysis_revision=row.analysis_revision + 1,
                    quantity=row.quantity + .01, notional=row.notional + 100)
            if index < 5 else row for index, row in enumerate(old.bid_levels))
        changes['recent_prints'] = tuple(
            replace(row, outcome='REJECTED' if iteration % 2 else 'FOLLOW_THROUGH',
                    outcome_direction=-1 if iteration % 2 else 1)
            if index < 10 else row for index, row in enumerate(old.recent_prints))
    elif case == 'rolling_prints' and old.recent_prints:
        tail = old.recent_prints[-1]
        new = tuple(replace(tail, sequence=tail.sequence + i,
                            received_monotonic=tail.received_monotonic + i / 10000,
                            event_time_ms=tail.event_time_ms + i)
                    for i in range(1, 21))
        changes['recent_prints'] = (old.recent_prints + new)[-len(old.recent_prints):]
    elif case == 'age_refresh':
        for side in ('bid_levels', 'ask_levels'):
            changes[side] = tuple(replace(row, age_seconds=row.age_seconds + .1,
                                         analysis_revision=row.analysis_revision + 1)
                                  for row in getattr(old, side))
    elif case == 'dense_levels':
        for side in ('bid_levels', 'ask_levels'):
            changes[side] = tuple(replace(
                row, quantity=row.quantity + .01, notional=row.notional + 100,
                delta_notional_5s=row.delta_notional_5s + 5,
                trade_notional_5s=row.trade_notional_5s + 100,
                signed_trade_notional_5s=row.signed_trade_notional_5s - 5,
                age_seconds=row.age_seconds + .1, analysis_revision=iteration,
                liquidity_intensity=.2 if iteration % 2 else .5,
                trade_intensity=.1 if iteration % 2 else .3,
            ) for row in getattr(old, side))
    snapshot = replace(old, **changes)
    return replace(frame, snapshot=snapshot,
                   build_started_mono=snapshot.generated_monotonic - .001,
                   build_completed_mono=snapshot.generated_monotonic)


def summary(samples):
    ordered = sorted(samples)
    return dict(median_ms=round(statistics.median(samples), 4),
                p95_ms=round(ordered[round((len(ordered) - 1) * .95)], 4),
                max_ms=round(max(samples), 4))


def measure(case, levels, prints, samples, coalesce_every):
    frame = initial_frame(levels, prints)
    analysis_sender, app_receiver = SnapshotEncoder(), SnapshotDecoder()
    dom_sender, dom_receiver = SnapshotEncoder(), SnapshotDecoder()

    def incremental(source, deliver):
        first, first_bytes = exchange(analysis_sender.encode(source, 7))
        restored = app_receiver.decode(first, 7)
        second_bytes = 0
        if deliver:
            second, second_bytes = exchange(dom_sender.encode(restored, 3))
            restored = dom_receiver.decode(second, 3)
        return restored, first_bytes + second_bytes

    _, seed_bytes = incremental(frame, True)
    baseline_ms, incremental_ms = [], []
    baseline_bytes = incremental_bytes = 0
    identical = True
    for iteration in range(1, samples + 1):
        frame = advance(frame, case, iteration)
        deliver = iteration % coalesce_every == 0 or iteration == samples
        started = time.perf_counter()
        restored, sent = exchange(frame)
        if deliver:
            restored, extra = exchange(restored)
            sent += extra
        baseline_ms.append((time.perf_counter() - started) * 1000)
        baseline_bytes += sent
        started = time.perf_counter()
        restored, sent = incremental(frame, deliver)
        incremental_ms.append((time.perf_counter() - started) * 1000)
        incremental_bytes += sent
        # Outside the measured interval: include compare=False fields, and
        # verify the final public model after frames skipped by the DOM queue.
        identical &= asdict(restored) == asdict(frame)
    return dict(
        case=case, samples=samples, complete_fields_preserved=identical,
        seed_bytes_two_hops=seed_bytes,
        full=dict(bytes=baseline_bytes, **summary(baseline_ms)),
        incremental=dict(bytes=incremental_bytes, **summary(incremental_ms)),
        transfer_reduction_pct=round((1 - incremental_bytes / baseline_bytes) * 100, 3),
        analysis_sent=analysis_sender.diagnostic_state(), dom_sent=dom_sender.diagnostic_state(),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--levels', type=int, default=120)
    parser.add_argument('--prints', type=int, default=2048)
    parser.add_argument('--samples', type=int, default=30)
    parser.add_argument('--coalesce-every', type=int, default=3)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not (0 <= args.levels <= 1000 and 0 <= args.prints <= 2048
            and args.samples >= 1 and args.coalesce_every >= 1):
        parser.error('Use bounded collection sizes and positive sample/coalescing counts')
    install_snapshot_reducers()
    report = dict(
        python=platform.python_version(), platform=platform.platform(),
        levels_per_side=args.levels, retained_prints=args.prints,
        dom_receives_every_nth_frame=args.coalesce_every,
        measurement='synthetic two-hop encoding/pickle/reconstruction; excludes kernel pipes and FPS',
        cases=[measure(case, args.levels, args.prints, args.samples, args.coalesce_every)
               for case in ('bbo_only', 'sparse_corrections', 'rolling_prints',
                            'age_refresh', 'dense_levels')],
    )
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    if not all(case['complete_fields_preserved'] for case in report['cases']):
        raise SystemExit('Transport measurement found a public-field mismatch')


if __name__ == '__main__':
    main()
