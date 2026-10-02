"""Wire compatibility and bounded hot-path work for market snapshot transport."""
from dataclasses import FrozenInstanceError, asdict, dataclass, field, replace
import dataclasses
import math
from multiprocessing.reduction import ForkingPickler
import pickle
import time

import pytest
from PySide6 import QtCore

from nightwatch.models import (
    OrderFlowDisplayLevel, OrderFlowPresentationFrame, OrderFlowSnapshot,
    OrderFlowTradePrint,
)
from nightwatch.orderbook.ipc import install_snapshot_reducers


def frame():
    trade = OrderFlowTradePrint(
        sequence=23, event_time_ms=1700000000000, received_monotonic=12.25,
        price=100.25, quantity=1.5, notional=150.375, aggressor_side='BUY',
        normal_notional=125.25, rpi_notional=25.125, relative_size=3.5,
        salience_class=2, reference_midpoint=100.15, outcome_threshold=.02,
        outcome='REJECTED', outcome_direction=-1,
    )
    bid = OrderFlowDisplayLevel(
        side='bid', price=100.0, quantity=2.5, notional=250.0,
        delta_notional_5s=-10.0, trade_notional_5s=22.5,
        signed_trade_notional_5s=-2.5, rpi_trade_notional_5s=5.0,
        age_seconds=7.5, persistence_ratio=.8, replenishments=3,
        state='RELOADING', liquidity_intensity=.9, delta_intensity=.25,
        trade_intensity=.5, cumulative_depth_notional=1000.0,
        depth_intensity=.6, liquidity_history_30s=(.1, .2, .3, .4, .5, .6, .7, .8),
        history_presence_30s=.75, history_peak_notional_30s=500.0,
        history_mean_notional_30s=300.0, buy_trade_notional_5s=10.0,
        sell_trade_notional_5s=12.5, buy_trade_count_5s=3,
        sell_trade_count_5s=5, largest_buy_trade_5s=7.0,
        largest_sell_trade_5s=9.0, rejected_buy_prints_5s=1,
        rejected_sell_prints_5s=2, recent_replenished_notional=20.0,
        recent_restacked_notional=30.0, trade_reload_count=2, restack_count=4,
        semantic_event_kind='reload', semantic_event_label='Reload',
        state_flags=('RELOAD', 'PERSISTENT'), persistent=True,
        analysis_revision=19, new_passive_added_notional=35.0,
        effective_cancelled_notional=40.0,
    )
    snapshot = OrderFlowSnapshot(
        symbol='BTCUSDT', sequence=31, generated_monotonic=13.0,
        ready=True, live=True, bbo_source='bookTicker', depth_age_seconds=.05,
        bbo_age_seconds=.01, trade_age_seconds=None,
        best_bid=100.0, best_ask=100.5, midpoint=100.25,
        large_trade_threshold=100.0, has_recent_activity=True,
        recent_prints=(trade,), bid_levels=(bid,),
        ask_levels=(replace(bid, side='ask', price=100.5),),
    )
    return OrderFlowPresentationFrame(snapshot, 12.9, 13.0)


@dataclass(frozen=True, slots=True)
class TradeWithMetadata(OrderFlowTradePrint):
    metadata: str = field(default='external', kw_only=True)


@pytest.mark.parametrize('protocol', [4, 5])
def test_snapshot_wire_roundtrip_preserves_every_field_and_shared_references(protocol):
    install_snapshot_reducers()
    source = frame()
    payload = (source, source.snapshot.recent_prints[0], {'generation': 7})
    restored, alias, metadata = pickle.loads(ForkingPickler.dumps(payload, protocol))
    # asdict includes compare=False fields such as age and analysis revision.
    assert asdict(restored) == asdict(source)
    assert alias is restored.snapshot.recent_prints[0]
    assert metadata == {'generation': 7}
    assert type(restored) is OrderFlowPresentationFrame
    assert type(restored.snapshot) is OrderFlowSnapshot
    assert type(alias) is OrderFlowTradePrint
    with pytest.raises(FrozenInstanceError):
        alias.outcome = 'UNRESOLVED'
    assert asdict(pickle.loads(pickle.dumps(source, protocol))) == asdict(source)


def test_snapshot_transport_does_not_inspect_schema_for_each_row(monkeypatch):
    install_snapshot_reducers()
    source = frame()
    expected = asdict(source)

    def unexpected_schema_scan(*args, **kwargs):
        raise AssertionError('Snapshot transport repeated dataclasses.fields()')

    with monkeypatch.context() as patch:
        patch.setattr(dataclasses, 'fields', unexpected_schema_scan)
        result = pickle.loads(ForkingPickler.dumps(source))
    assert asdict(result) == expected


def test_unregistered_subclass_keeps_keyword_only_state():
    install_snapshot_reducers()
    fields = asdict(frame().snapshot.recent_prints[0])
    source = TradeWithMetadata(**fields, metadata='retained')
    restored = pickle.loads(ForkingPickler.dumps(source))
    assert type(restored) is TradeWithMetadata
    assert asdict(restored) == asdict(source)


def test_consecutive_sends_preserve_mutations_and_corrected_outcomes():
    install_snapshot_reducers()
    source = frame()
    commands = [('snapshot', (4, source)), ('add_depth', [7, [(100.0, 2.0)]])]
    first = pickle.loads(ForkingPickler.dumps(commands))
    commands[1][1][1][0] = (100.0, 3.0)
    corrected = replace(source.snapshot.recent_prints[0], outcome='FOLLOW_THROUGH',
                        outcome_direction=1)
    commands[0] = ('snapshot', (5, replace(source, snapshot=replace(
        source.snapshot, recent_prints=(corrected,)))))
    second = pickle.loads(ForkingPickler.dumps(commands))
    assert first[1][1][1] == [(100.0, 2.0)]
    assert second[1][1][1] == [(100.0, 3.0)]
    assert second[0][1][0] == 5
    assert second[0][1][1].snapshot.recent_prints[0].outcome_direction == 1
    assert first[0][1][1].snapshot.recent_prints[0].outcome_direction == -1


class EchoProcess:
    def __init__(self, options):
        pass

    def step(self, commands, lease):
        return {'commands': commands, 'lease': lease, 'next_at': math.inf}

    def close(self):
        pass


def test_spawned_process_link_exchanges_snapshots_and_shuts_down(qapp):
    from nightwatch.orderbook.backend import _OrderBookProcessLink
    link = _OrderBookProcessLink(EchoProcess, {}, ordered=True)
    received, errors = [], []
    link.ready.connect(received.append)
    link.failed.connect(errors.append)
    source = frame()
    link.submit('snapshot', (8, source))
    link.submit('reset_model', (9, 'ETHUSDT', .01, 1000.0))
    link.enable(True)
    try:
        deadline = time.monotonic() + 10.0
        while not received and not errors and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(.002)
        assert not errors
        assert received, 'Spawned worker did not reply'
        commands = received[0]['commands']
        assert [name for name, _ in commands] == ['snapshot', 'reset_model']
        assert asdict(commands[0][1][1]) == asdict(source)
        assert commands[1][1] == (9, 'ETHUSDT', .01, 1000.0)
        link.consumed()
    finally:
        link.close()
        link._thread.join(timeout=3.0)
        assert not link._thread.is_alive()
        link.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
