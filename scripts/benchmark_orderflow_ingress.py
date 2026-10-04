"""Offline parsing, local-book publication and two-hop IPC measurements.

NIGHTWATCH_BENCH_SOURCE selects the prior checkout. The optional parser-process
candidate is an isolated benchmark, not a production transport. No exchange
connection or order is created. Timings exclude GUI painting and desktop FPS.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import hashlib
import json
import multiprocessing
from multiprocessing.reduction import ForkingPickler
import os
from pathlib import Path
import pickle
import platform
import statistics
import subprocess
import sys
import time
import types

SOURCE = Path(os.environ.get('NIGHTWATCH_BENCH_SOURCE', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(SOURCE / 'scripts'))
if os.environ.get('NIGHTWATCH_QT_SITE_PACKAGES'):
    sys.path.append(os.environ['NIGHTWATCH_QT_SITE_PACKAGES'])
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from PySide6 import QtCore
from nightwatch.market.data import _DepthParserWorker, _decode_socket_payload
from nightwatch.orderbook.ipc import install_snapshot_reducers
from nightwatch.orderbook import ipc as current_codec
from benchmark_orderflow_ipc import advance, initial_frame


def summary(samples):
    ordered = sorted(samples)
    return dict(median_ms=round(statistics.median(samples), 5),
                p95_ms=round(ordered[round((len(ordered) - 1) * .95)], 5),
                max_ms=round(max(samples), 5))


def timed(function, samples=128):
    result, values = None, []
    for index in range(samples + 8):
        started = time.perf_counter()
        result = function(index)
        if index >= 8:
            values.append((time.perf_counter() - started) * 1000)
    return result, summary(values)


def parser_messages():
    return dict(
        trade=dict(e='aggTrade', s='BTCUSDT', a=1, p='10000', q='.01', nq='.009', m=False, T=1700000000000),
        book_ticker=dict(e='bookTicker', s='BTCUSDT', b='10000', a='10000.1', B='2', A='2', u=1000),
        ticker_array=[dict(e='24hrMiniTicker', s=f'PAIR{i}USDT', c='100', o='99', h='105', l='98', q='1000000', v='10000') for i in range(900)],
        mark_array=[dict(e='markPriceUpdate', s=f'PAIR{i}USDT', p='100', i='100', P='100', r='.0001', T=1700000000000, E=1700000000000) for i in range(900)],
    )


TRACKED = tuple(f'PAIR{i}USDT' for i in range(50))
VALID = frozenset(f'PAIR{i}USDT' for i in range(900))


def decode(message):
    return _decode_socket_payload('market', message, 'BTCUSDT', '1m', TRACKED, VALID)


def parser_candidate(connection):
    connection.send('ready')
    try:
        while True:
            messages = connection.recv()
            if messages is None:
                return
            connection.send(tuple(decode(message) for message in messages))
    finally:
        connection.close()


def parse_measurements(process_samples):
    messages = {name: json.dumps(dict(stream='feed', data=data), separators=(',', ':'))
                for name, data in parser_messages().items()}
    results = []
    for name, message in messages.items():
        _data, parsing = timed(lambda _index: json.loads(message))
        packets, complete = timed(lambda _index: decode(message))
        digest = hashlib.sha256(json.dumps(packets, sort_keys=True).encode()).hexdigest()
        results.append(dict(case=name, message_bytes=len(message.encode()), json=parsing,
                            parse_and_classify=complete, packet_digest=digest))
    candidate = None
    if process_samples:
        context = multiprocessing.get_context('spawn')
        connection, child = context.Pipe()
        process = context.Process(target=parser_candidate, args=(child,), daemon=True)
        started = time.perf_counter()
        process.start()
        child.close()
        if not connection.poll(10) or connection.recv() != 'ready':
            process.terminate()
            raise RuntimeError('Parser candidate did not start')
        startup_ms = (time.perf_counter() - started) * 1000
        workloads = []
        try:
            for name in ('trade', 'book_ticker', 'ticker_array', 'mark_array'):
                batch_sizes = (1, 32) if name == 'trade' else (1,)
                for batch_size in batch_sizes:
                    batch = (messages[name],) * batch_size
                    expected, direct = timed(lambda _index: tuple(decode(message) for message in batch), process_samples)

                    def exchange(_index):
                        connection.send(batch)
                        if not connection.poll(5):
                            raise RuntimeError('Parser candidate reply timed out')
                        return connection.recv()

                    actual, isolated = timed(exchange, process_samples)
                    workloads.append(dict(case=name, batch_size=batch_size, direct=direct,
                                          spawned_roundtrip=isolated, complete_packets_preserved=actual == expected))
            candidate = dict(startup_ms=round(startup_ms, 3), workloads=workloads,
                             scope='one spawned parser, pipe send/receive and complete packet reconstruction; sequential latency, not GUI contention')
        finally:
            try:
                connection.send(None)
            except (EOFError, BrokenPipeError, OSError):
                pass
            connection.close()
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
    return dict(parsing=results, process_candidate=candidate)


def depth_measurement(levels, samples=128):
    worker = _DepthParserWorker()
    worker.reset_epoch(1, 0, 'BTCUSDT')
    worker.set_analysis_publish_levels(levels)
    latest = []
    worker.parsed.connect(lambda _epoch, _revision, payload: latest.append(payload))
    bids = [(10000 - i * .1, 2) for i in range(levels)]
    asks = [(10000.1 + i * .1, 2) for i in range(levels)]
    worker.seed_snapshot(1, 0, dict(s='BTCUSDT', lastUpdateId=1000, bids=bids, asks=asks))
    messages = [json.dumps(dict(stream='btcusdt@depth@100ms', data=dict(
        e='depthUpdate', s='BTCUSDT', U=1000 + i, u=1001 + i, pu=1000 + i,
        b=[(str(price), str(2 + i % 2)) for price, _qty in bids[:5]],
        a=[(str(price), str(2 + i % 2)) for price, _qty in asks[:5]])))
        for i in range(samples + 8)]
    _event, parsing = timed(lambda index: worker._decode_delta(messages[index], 'BTCUSDT', (0, 0, 0, 0)), samples)

    def publish(index):
        latest.clear()
        worker.parse(1, messages[index], 'BTCUSDT', (0, 0, 0, 0))

    _none, publication = timed(publish, samples)
    payload = latest[-1]
    rows = payload['_analysis_bids'], payload['_analysis_asks']
    expected_bids = [(price, 2 + (samples + 7) % 2 if i < 5 else qty) for i, (price, qty) in enumerate(bids)]
    expected_asks = [(price, 2 + (samples + 7) % 2 if i < 5 else qty) for i, (price, qty) in enumerate(asks)]
    command = (7, *rows, payload['_analysis_revision'], None)
    _restored, serialization = timed(lambda _index: pickle.loads(ForkingPickler.dumps(command, 5)), samples)
    return dict(levels_per_side=levels, changed_rows_per_side=5, decode=parsing,
                parse_reconstruct_publish=publication, raw_depth_roundtrip=serialization,
                book_rows_preserved=rows == (expected_bids, expected_asks))


def modified_frame(frame, case, iteration):
    if case in ('shape_outlier', 'membership', 'reseed'):
        frame = advance(frame, 'age_refresh' if case == 'shape_outlier' else 'bbo_only', iteration)
        snapshot = frame.snapshot
        if case == 'shape_outlier':
            bids = tuple(replace(row, quantity=row.quantity + .1,
                                 notional=row.notional + row.price * .1,
                                 signed_trade_notional_5s=row.signed_trade_notional_5s + 9)
                         if i == len(snapshot.bid_levels) // 2 else row
                         for i, row in enumerate(snapshot.bid_levels))
            frame = replace(frame, snapshot=replace(snapshot, bid_levels=bids))
        elif case == 'membership':
            bids, asks = snapshot.bid_levels, snapshot.ask_levels
            bid_price, ask_price = bids[-1].price - .1, asks[-1].price + .1
            bids = bids[1:] + (replace(bids[-1], price=bid_price, notional=bid_price * bids[-1].quantity),)
            asks = asks[1:] + (replace(asks[-1], price=ask_price, notional=ask_price * asks[-1].quantity),)
            frame = replace(frame, snapshot=replace(snapshot, bid_levels=bids, ask_levels=asks,
                                                    best_bid=bids[0].price, best_ask=asks[0].price))
        return frame
    return advance(frame, case, iteration)


def pipeline(codec):
    return ((codec.SnapshotEncoder(), codec.SnapshotDecoder(), 7),
            (codec.SnapshotEncoder(), codec.SnapshotDecoder(), 3))


def exchange_frame(source, pipes, seed):
    current = {name: 0.0 for name in ('encode', 'pickle', 'reconstruct', 'total')}
    result, transferred = source, 0
    started = time.perf_counter()
    for sender, receiver, generation in pipes:
        stage_started = time.perf_counter()
        patch = sender.encode(result, generation, force_seed=seed)
        current['encode'] += (time.perf_counter() - stage_started) * 1000
        stage_started = time.perf_counter()
        packet = ForkingPickler.dumps(patch, 5)
        patch = pickle.loads(packet)
        transferred += len(packet)
        current['pickle'] += (time.perf_counter() - stage_started) * 1000
        stage_started = time.perf_counter()
        result = receiver.decode(patch, generation)
        current['reconstruct'] += (time.perf_counter() - stage_started) * 1000
    current['total'] = (time.perf_counter() - started) * 1000
    return result, current, transferred


def ipc_measurement(case, levels, samples):
    source = initial_frame(levels, 2048)
    pipes = pipeline(current_codec)
    stages = {name: [] for name in ('encode', 'pickle', 'reconstruct', 'total')}
    wire_bytes = seed_bytes = 0
    preserved = True
    for iteration in range(samples + 1):
        if iteration:
            source = modified_frame(source, case, iteration)
        seed = case == 'reseed' and iteration % 8 == 0
        result, current, transferred = exchange_frame(source, pipes, seed)
        if iteration:
            wire_bytes += transferred
            for name in stages:
                stages[name].append(current[name])
        else:
            seed_bytes = transferred
        # Include compare=False fields and all nested immutable records.
        preserved &= asdict(result) == asdict(source)
    return dict(case=case, levels_per_side=levels, samples=samples, complete_fields_preserved=preserved,
                seed_bytes=seed_bytes, update_bytes=wire_bytes,
                stages={name: summary(values) for name, values in stages.items()})


def load_baseline(reference):
    source = subprocess.check_output(['git', 'show', f'{reference}:nightwatch/orderbook/ipc.py'],
                                     cwd=SOURCE, text=True)
    name = 'nightwatch.orderbook._ipc_benchmark_baseline'
    module = types.ModuleType(name)
    sys.modules[name] = module
    # Execute the already committed baseline under an isolated module name.
    # Public models stay identical; private DTOs and their reducers are separate.
    exec(compile(source, '<committed IPC benchmark baseline>', 'exec'), module.__dict__)
    return module


def paired_ipc_measurement(baseline, case, levels, samples):
    codecs = dict(before=baseline, after=current_codec)
    pipes = {name: pipeline(codec) for name, codec in codecs.items()}
    stages = {name: {stage: [] for stage in ('encode', 'pickle', 'reconstruct', 'total')}
              for name in codecs}
    source = initial_frame(levels, 2048)
    preserved = True
    for iteration in range(samples + 1):
        if iteration:
            source = modified_frame(source, case, iteration)
        seed = case == 'reseed' and iteration % 8 == 0
        outputs = []
        for name in (('before', 'after') if iteration % 2 else ('after', 'before')):
            # Reducer registration is outside the interval and affects only
            # this single-threaded harness. Alternate order to reduce drift.
            codecs[name].install_snapshot_reducers()
            result, current, _transferred = exchange_frame(source, pipes[name], seed)
            outputs.append(result)
            if iteration:
                for stage in stages[name]:
                    stages[name][stage].append(current[stage])
        expected = asdict(source)
        preserved &= all(asdict(result) == expected for result in outputs)
    current_codec.install_snapshot_reducers()
    timings = {name: {stage: summary(values) for stage, values in timings.items()}
               for name, timings in stages.items()}
    before, after = timings['before']['total']['median_ms'], timings['after']['total']['median_ms']
    return dict(case=case, levels_per_side=levels, samples=samples, complete_fields_preserved=preserved,
                median_reduction_pct=round((1 - after / before) * 100, 2), **timings)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', default='working-tree')
    parser.add_argument('--samples', type=int, default=24)
    parser.add_argument('--process-samples', type=int, default=32)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--paired-ref', help='Compare this checkout with a committed IPC file in alternating order')
    args = parser.parse_args()
    if args.samples < 8 or args.process_samples < 0:
        parser.error('Use at least eight IPC samples and a non-negative candidate count')
    install_snapshot_reducers()
    application = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
    parser_results = parse_measurements(args.process_samples)
    baseline = load_baseline(args.paired_ref) if args.paired_ref else None
    report = dict(label=args.label, python=platform.python_version(), qt=QtCore.qVersion(),
                  platform=platform.platform(), gil_switch_seconds=sys.getswitchinterval(),
                  scope='synthetic CPU/transport timings; no exchange, GUI paint or desktop-FPS measurement',
                  **parser_results,
                  depth=[depth_measurement(levels) for levels in (120, 1000)],
                  ipc=[ipc_measurement(case, levels, args.samples) for levels in (120, 1000)
                       for case in ('bbo_only', 'sparse_corrections', 'rolling_prints', 'age_refresh',
                                    'dense_levels', 'shape_outlier', 'membership', 'reseed')],
                  paired_ref=args.paired_ref,
                  paired_ipc=[paired_ipc_measurement(baseline, case, levels, args.samples)
                              for levels in (120, 1000)
                              for case in ('bbo_only', 'sparse_corrections', 'rolling_prints', 'age_refresh',
                                           'dense_levels', 'shape_outlier', 'membership', 'reseed')]
                              if baseline else [])
    text = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.write_text(text)
    print(text, end='')
    if (not all(case['complete_fields_preserved'] for case in report['ipc'])
            or not all(case['complete_fields_preserved'] for case in report['paired_ipc'])
            or not all(case['book_rows_preserved'] for case in report['depth'])
            or (parser_results['process_candidate'] and not all(case['complete_packets_preserved'] for case in parser_results['process_candidate']['workloads']))):
        raise SystemExit('Measurement did not preserve complete market data')


if __name__ == '__main__':
    main()
