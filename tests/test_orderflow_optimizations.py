"""Correctness and avoided-work checks for the four order-flow changes.

Wall-clock comparisons live in the offline benchmark scripts. These tests
check data, recovery, pixels and bounded work without machine-speed thresholds.
"""
from dataclasses import asdict, replace
import json
from multiprocessing.reduction import ForkingPickler
import os
from pathlib import Path
import pickle
import subprocess
import sys

import pytest
from PySide6 import QtCore

from nightwatch.orderbook.ipc import (
    SnapshotDecoder, SnapshotEncoder, SnapshotSeedRequired,
    install_snapshot_reducers,
)
from nightwatch.orderbook.orderbook_ui import (
    TradesTapeWidget, _DomAggregation, _DomRasterWorkerCanvas,
    aggregate_order_flow_snapshot,
)
from nightwatch.orderbook.revisions import OrderFlowRevisionTracker
from scripts.benchmark_orderflow_ipc import advance, initial_frame
from scripts import benchmark_orderflow_regions as regions
from scripts.profile_orderflow_tape import capture_profile
from test_trade_tape import snapshot, trade, wait_for


ROOT = Path(__file__).resolve().parents[1]


def wire(value):
    return pickle.loads(ForkingPickler.dumps(value, 5))


@pytest.mark.parametrize('levels', [120, 1000])
@pytest.mark.parametrize('case', ['bbo_only', 'sparse_corrections', 'rolling_prints',
                                 'age_refresh', 'dense_levels'])
def test_two_hops_preserve_all_fields_across_coalesced_presentation(levels, case):
    install_snapshot_reducers()
    source = initial_frame(levels, 2048)
    first_sender, first_receiver = SnapshotEncoder(), SnapshotDecoder()
    second_sender, second_receiver = SnapshotEncoder(), SnapshotDecoder()
    previous = None
    for index in range(13):
        if index:
            source = advance(source, case, index)
        timing = {'input': index, 'nested': {'duration': .001 * index}}
        incoming, restored_timing = first_receiver.decode(
            wire(first_sender.encode((source, timing), 7)), 7)
        assert asdict(incoming) == asdict(source)
        assert restored_timing == timing
        if previous is not None and case == 'bbo_only':
            for field in ('bid_levels', 'ask_levels', 'recent_prints'):
                assert getattr(incoming.snapshot, field) is getattr(previous.snapshot, field)
        previous = incoming
        if index % 3 == 0:
            displayed = second_receiver.decode(wire(second_sender.encode(incoming, 3)), 3)
            assert asdict(displayed) == asdict(source)
    if case == 'bbo_only':
        counts = first_sender.diagnostic_state()
        assert counts['level_records'] == levels * 2
        assert counts['print_records'] == 2048
        assert counts['seeds'] == 1 and counts['deltas'] == 12


@pytest.mark.parametrize('levels', [120, 1000])
def test_sparse_shape_hint_preserves_unsampled_outlier_fields(levels):
    sender, receiver = SnapshotEncoder(), SnapshotDecoder()
    source = initial_frame(levels, 1)
    receiver.decode(wire(sender.encode(source, 7)), 7)
    source = advance(source, 'age_refresh', 1)
    bids = list(source.snapshot.bid_levels)
    middle = levels // 2
    bids[middle] = replace(bids[middle], quantity=7, signed_trade_notional_5s=-57,
                           liquidity_history_30s=(.7, .2))
    source = replace(source, snapshot=replace(source.snapshot, bid_levels=tuple(bids)))
    result = receiver.decode(wire(sender.encode(source, 7)), 7)
    assert asdict(result) == asdict(source)


def test_rejected_collection_does_not_advance_sender_or_receiver_base():
    sender, receiver = SnapshotEncoder(), SnapshotDecoder()
    source = initial_frame(120, 3)
    seed = sender.encode(source, 7)
    receiver.decode(wire(seed), 7)
    changed = advance(source, 'sparse_corrections', 1)
    invalid = replace(changed, snapshot=replace(changed.snapshot,
        recent_prints=(changed.snapshot.recent_prints[0],) * 2))
    before = sender.diagnostic_state()
    with pytest.raises(ValueError, match='Duplicate'):
        sender.encode(invalid, 7)
    assert sender.diagnostic_state() == before
    delta = wire(sender.encode(changed, 7))
    assert delta.base_sequence == source.snapshot.sequence
    # Corrupt the final collection after earlier valid corrections were staged.
    patches = list(delta.collections)
    patches[-1] = replace(patches[-1], order=(23, 23))
    before = receiver.diagnostic_state()
    with pytest.raises(SnapshotSeedRequired):
        receiver.decode(replace(delta, collections=tuple(patches)), 7)
    assert receiver.diagnostic_state() == before
    assert asdict(receiver.decode(delta, 7)) == asdict(changed)


def test_missing_base_and_same_symbol_reset_recover_without_replaying_inputs():
    sender, receiver = SnapshotEncoder(), SnapshotDecoder()
    source = initial_frame(120, 3)
    receiver.decode(wire(sender.encode(source, 7)), 7)
    changed = advance(source, 'sparse_corrections', 1)
    delta = wire(sender.encode(changed, 7))
    lost = SnapshotDecoder()
    with pytest.raises(SnapshotSeedRequired):
        lost.decode(delta, 7)
    assert asdict(lost.decode(wire(sender.encode(changed, 7, force_seed=True)), 7)) == asdict(changed)
    reset = replace(changed, snapshot=replace(changed.snapshot, sequence=1))
    reset_packet = sender.encode(reset, 8)
    assert reset_packet.base_sequence is None
    with pytest.raises(SnapshotSeedRequired):
        receiver.decode(wire(reset_packet), 7)
    assert asdict(receiver.decode(wire(reset_packet), 8)) == asdict(reset)


def test_revisions_separate_amounts_outcomes_age_and_same_symbol_sessions():
    tracker = OrderFlowRevisionTracker()
    source = initial_frame(120, 3)
    old = source.snapshot
    first = tracker.update(old.bid_levels, old.ask_levels, old.recent_prints)
    aged = advance(source, 'age_refresh', 1).snapshot
    second = tracker.update(aged.bid_levels, aged.ask_levels, aged.recent_prints)
    assert second.bid_levels > first.bid_levels and second.ask_levels > first.ask_levels
    assert second.amounts == first.amounts and second.recent_prints == first.recent_prints
    corrected = tuple(replace(record, outcome='FOLLOW_THROUGH', outcome_direction=1)
                      for record in aged.recent_prints)
    third = tracker.update(aged.bid_levels, aged.ask_levels, corrected)
    assert third.recent_prints > second.recent_prints
    assert (third.bid_levels, third.ask_levels, third.amounts) == (
        second.bid_levels, second.ask_levels, second.amounts)
    tracker.reset()
    restarted = tracker.update(aged.bid_levels, aged.ask_levels, aged.recent_prints)
    assert restarted.stream != first.stream


@pytest.mark.parametrize('metadata', [False, True])
def test_aggregation_reuses_reseeded_levels_and_invalidates_signed_flow(metadata):
    tracker, worker = OrderFlowRevisionTracker(), _DomAggregation()
    source = initial_frame(120, 3).snapshot
    context = (7, source.symbol, 5, .1)
    for index in range(4):
        versions = tracker.update(source.bid_levels, source.ask_levels, source.recent_prints)
        outgoing = replace(source, sequence=index + 1,
                           component_revisions=versions if metadata else None)
        # Complete pickle deliberately destroys tuple identity on each frame.
        incoming = wire(outgoing)
        _source, display, _started, _completed = worker.prepare(context, incoming)
        assert asdict(display) == asdict(aggregate_order_flow_snapshot(incoming, 5, .1))
    assert worker.reused == 3
    bids = (replace(source.bid_levels[0], signed_trade_notional_5s=-500), *source.bid_levels[1:])
    versions = tracker.update(bids, source.ask_levels, source.recent_prints)
    incoming = wire(replace(source, sequence=5, bid_levels=bids,
                            component_revisions=versions if metadata else None))
    _source, display, _started, _completed = worker.prepare(context, incoming)
    assert worker.rebuilt == 2
    assert asdict(display) == asdict(aggregate_order_flow_snapshot(incoming, 5, .1))


def test_width_formatting_is_avoided_for_ages_and_far_depth(qapp, monkeypatch):
    canvas, tracker = _DomRasterWorkerCanvas({}), OrderFlowRevisionTracker()
    source = initial_frame(1000, 1)
    calls = []
    original = canvas._row_amount
    monkeypatch.setattr(canvas, '_row_amount', lambda *args, **kwargs: (
        calls.append(1), original(*args, **kwargs))[1])
    try:
        for index in range(7):
            if index:
                source = advance(source, 'age_refresh', index)
            snapshot = source.snapshot
            snapshot = replace(snapshot, component_revisions=tracker.update(
                snapshot.bid_levels, snapshot.ask_levels, snapshot.recent_prints))
            canvas._required_amount_lane_width(wire(snapshot))
        assert len(calls) == 128  # Initial visible amounts only.
        bids = (*snapshot.bid_levels[:-1], replace(snapshot.bid_levels[-1], quantity=9, notional=90000))
        snapshot = replace(snapshot, bid_levels=bids, component_revisions=tracker.update(
            bids, snapshot.ask_levels, snapshot.recent_prints))
        canvas._required_amount_lane_width(wire(snapshot))
        assert len(calls) == 128
        canvas.set_value_mode('base')
        canvas._required_amount_lane_width(wire(snapshot))
        assert len(calls) == 256  # Unit changes require fresh measurement.
    finally:
        canvas._aggregation_job.close()
        canvas.close()


@pytest.mark.parametrize('dpr', [1, 1.1, 1.25, 1.5, 1.75, 2])
@pytest.mark.parametrize('case', ['bbo_bands', 'same_row', 'moving_row', 'disjoint_rows',
                                 'held_lease', 'unpublished_reply', 'full_churn',
                                 'fragmented', 'resize_reset'])
def test_regional_buffers_preserve_every_pixel_and_lease(qapp, dpr, case):
    result = regions.measure(qapp, case, dpr, 8)
    assert result['complete_buffer_pixels_preserved']
    assert result['leased_pixels_immutable']
    assert result['gui_pixels_match_full_native_blit']
    assert result['no_paint_reply_republished']
    assert result['max_region_rects'] <= 64
    if case in ('bbo_bands', 'same_row', 'moving_row'):
        assert result['copied_bytes'] < result['baseline_copy_bytes'] * .1
        assert result['submitted_blit_pixels'] < result['baseline_blit_pixels'] * .1


@pytest.mark.parametrize('dpr', [1.1, 2])
@pytest.mark.parametrize('case', ['legacy_lease', 'stale_revision'])
def test_unknown_leases_conservatively_preserve_pixels(qapp, dpr, case):
    result = regions.measure(qapp, case, dpr, 8)
    assert result['complete_buffer_pixels_preserved']
    assert result['leased_pixels_immutable']
    assert result['gui_pixels_match_full_native_blit']


def test_tape_captures_evicted_trades_and_recovers_stalled_views():
    result = capture_profile()
    checks = [value for value in result.values() if isinstance(value, bool)]
    assert checks and all(checks)
    assert result['hidden_view_frames'] == 0
    assert result['recent_rows_after_expiry'] == 0
    assert result['retained_history_rows'] == [500, 1]


def test_quantity_switch_preserves_small_amount_without_new_tape_frame(qapp):
    widget = TradesTapeWidget({})
    try:
        widget.set_market('ETHUSDT', tick_size=.01)
        widget.resize(450, 250)
        widget.set_panel_active(True)
        widget.show()
        widget.set_order_flow_snapshot(snapshot([trade(1, 100, quantity=1e-8)]))
        wait_for(qapp, lambda: widget.model.rowCount() == 1)
        count = widget._tape_source.delivered_frames
        widget.set_value_mode('base')
        widget._refresh_table()
        assert widget.model.index(0, 1).data() == '0.00000001'
        assert widget._tape_source.delivered_frames == count
        widget.set_value_mode('quote')
        widget._refresh_table()
        assert widget.model.index(0, 1).data() == '$0.000001'
    finally:
        widget.close()
        if widget._owned_tape_source:
            widget._owned_tape_source.close()
            widget._owned_tape_source._worker.thread.join(timeout=2)
        widget.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


@pytest.mark.parametrize('kind', ['tape', 'regions'])
def test_actual_spawned_workers_complete_recovery_sessions(tmp_path, kind):
    output = tmp_path / f'{kind}.json'
    environment = dict(os.environ, NIGHTWATCH_BENCH_SOURCE=str(ROOT),
                       NIGHTWATCH_QT_SITE_PACKAGES=str(Path(QtCore.__file__).parents[1]),
                       QT_SCALE_FACTOR='1.25', QT_QPA_PLATFORM='offscreen')
    result = subprocess.run([sys.executable, str(ROOT / 'scripts' / f'profile_orderflow_{kind}.py'),
                             '--output', str(output)], cwd=ROOT, env=environment,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    if kind == 'tape':
        assert all(value for value in report['capture'].values() if isinstance(value, bool))
        session = report['session']
        assert not session['errors'] and session['completed_phases'] == 14
        assert all(value for value in session.values() if isinstance(value, bool))
    else:
        assert not report['errors'] and not report['renderer_error']
        assert report['latest_displayed_sequence'] == report['latest_source_sequence']
        assert report['retained_tape_rows'] == [40, 40]
        assert not report['awaiting_raster_ack']
