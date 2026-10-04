import json
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from nightwatch.models import SymbolRules
from nightwatch.trading.gateway import _PlacementJournal


def entry_record(**changes):
    return {'kind': 'entry', 'client_id': 'entry', 'method': 'order.place',
            'order': {'symbol': 'BTCUSDT', 'side': 'BUY'},
            'context': {'entry': {'symbol': 'BTCUSDT', 'side': 'BUY'},
                        'protections': {'sl': [{'percent': 100, 'price': 90}]}}, **changes}


def test_tranche_is_durable_before_any_transmission_and_keeps_status(gateway, tmp_path, monkeypatch):
    gateway._journal = _PlacementJournal(str(tmp_path / 'orders.sqlite3'), 'mainnet:test')
    gateway._protection_records['entry'] = entry_record(last_status='FILLED', _status_received_mono=100)
    tasks = []
    sent = []
    monkeypatch.setattr(gateway, '_launch_task', lambda f, done, failed, pool=None: tasks.append((f, done)) or Mock())
    monkeypatch.setattr(gateway, 'submit_order', lambda order, context: sent.append(context['client_id']))
    legs = [({'symbol': 'BTCUSDT'}, {'client_id': name, 'entry_client_id': 'entry', 'protective_leg': True})
            for name in ['stop', 'tp']]
    gateway.submit_protection_tranche({'client_id': 'entry', 'entry': {'symbol': 'BTCUSDT'}, 'protected_quantity': 2}, legs)
    assert not sent
    assert gateway._protection_reserved_slots == 2
    result = tasks[0][0]()  # Actual SQLite transaction, followed by queued GUI completion.
    saved = dict(gateway._journal.run('load')['protections'])
    assert set(saved) == {'entry', 'stop', 'tp'}
    assert json.loads(saved['entry'])['last_status'] == 'FILLED'
    assert json.loads(saved['entry'])['_status_received_mono'] == 100
    assert not sent
    tasks[0][1](result)
    assert sent == ['stop', 'tp'] and gateway._protection_reserved_slots == 0


def test_recovery_saves_terminal_status_and_confirms_flat_position(gateway, tmp_path, monkeypatch):
    gateway._journal = _PlacementJournal(str(tmp_path / 'orders.sqlite3'), 'mainnet:test')
    gateway.account_loaded = True
    gateway._last_snapshot_mono = time.monotonic() - 1
    gateway._protection_records['entry'] = entry_record(last_status='NEW')
    monkeypatch.setattr(gateway.rest, 'query_order_by_client_id', lambda *a, **k: {'status': 'FILLED', 'executedQty': '2'})
    tasks, refreshed, delivered = [], [], []
    monkeypatch.setattr(gateway, '_launch_task', lambda f, done, failed, pool=None: tasks.append((f, done)) or Mock())
    monkeypatch.setattr(gateway, 'refresh_account', lambda *a, **k: refreshed.append(k))
    gateway.protections_recovered.connect(delivered.append)
    gateway._recover_protections()
    tasks[0][1](tasks[0][0]())
    assert gateway._protection_records['entry']['last_status'] == 'FILLED'
    tasks[1][0]()
    assert json.loads(dict(gateway._journal.run('load')['protections'])['entry'])['last_status'] == 'FILLED'
    assert refreshed == [{'all_open_orders': True, 'follow_up': True}]
    assert not delivered and gateway._protection_recovery_pending
    gateway._last_snapshot_mono = time.monotonic()
    gateway._last_account_snapshot = {'ordersScope': 'ALL', 'orders': [], 'algoOrders': [],
                                      '_read_started_mono': gateway._last_snapshot_mono}
    # The follow-up account read resumes recovery before retiring saved plans.
    gateway._recover_protections()
    tasks[-1][1](tasks[-1][0]())
    assert not gateway._protection_records and not gateway._protection_recovery_pending


def test_rest_read_started_before_fill_cannot_retire_protection(gateway, monkeypatch):
    gateway._journal = object()
    gateway._protection_records['entry'] = entry_record(last_status='FILLED', _status_received_mono=20)
    monkeypatch.setattr(gateway, '_launch_task', lambda *a: (_ for _ in ()).throw(AssertionError('Must retain newer intent')))
    gateway._retire_closed_protections({'ordersScope': 'ALL', 'orders': [], '_read_started_mono': 10})
    assert 'entry' in gateway._protection_records


def protection_ui():
    from nightwatch.app.main_window import MainWindow
    sent, closes, states = [], [], []
    client_ids = iter(['leg-1', 'leg-2', 'leg-3', 'leg-4'])
    gateway = SimpleNamespace(_protection_recovery_pending=False,
                              client_order_id=lambda prefix: next(client_ids),
                              submit_protection_tranche=lambda plan, legs: sent.append((plan, legs)),
                              has_open_position=lambda symbol, position_side=None: True)
    ui = SimpleNamespace(trading_gateway=gateway, submitted_protection_clients=set(), pending_protections={},
                         active_protection_legs={}, symbol_rules={},
                         _set_ticket_protection_state=lambda symbol, state: states.append(state),
                         _start_emergency_close=lambda context, error: closes.append((context, error)),
                         alerts_panel=SimpleNamespace(append_alert=lambda *a: None),
                         statusBar=lambda: SimpleNamespace(showMessage=lambda *a: None))
    ui._submit_protection_plan = lambda *a, **k: MainWindow._submit_protection_plan(ui, *a, **k)
    return ui, sent, closes, states


def plan():
    return {'client_id': 'entry', 'entry': {'symbol': 'BTCUSDT', 'side': 'BUY', 'positionSide': 'BOTH'},
            'protections': {'sl': [{'price': '90', 'percent': 100}]},
            'rules': SymbolRules(market_step='0.1', min_market_qty=.1, tick_size='1')}


def test_small_partial_fill_accumulates_and_duplicate_fill_does_not_reallocate(qapp):
    ui, sent, closes, states = protection_ui()
    context = plan()
    ui._submit_protection_plan(context, .05, terminal=False)
    assert not sent and not closes and 'WAITING FOR MINIMUM' in states[-1]
    ui._submit_protection_plan(context, .2, terminal=False)
    ui._submit_protection_plan(context, .2, terminal=False)
    assert len(sent) == 1 and sent[0][1][0][0]['quantity'] == '0.2'
    ui._submit_protection_plan(context, .3, terminal=True)
    assert len(sent) == 2 and sent[1][1][0][0]['quantity'] == '0.1'
    assert not closes and 'entry' in ui.submitted_protection_clients


def test_terminal_dust_is_explicit_and_requests_one_fail_safe(qapp):
    ui, sent, closes, states = protection_ui()
    ui._submit_protection_plan(plan(), .05, terminal=True)
    assert not sent and len(closes) == 1
    assert closes[0][0]['quantity'] == .05


def test_recovery_preserves_newer_allocation_and_skips_closed_entry(qapp):
    from nightwatch.app.main_window import MainWindow
    ui, sent, closes, _states = protection_ui()
    context = plan()
    ui.pending_protections['entry'] = {**context, 'filled_quantity': 2, 'protected_quantity': 2}
    saved = {'kind': 'entry', 'client_id': 'entry', 'context': {**context, 'protected_quantity': 1},
             'result': {'status': 'PARTIALLY_FILLED', 'executedQty': '2'}}
    MainWindow._restore_saved_protections(ui, {'records': [saved], 'errors': []})
    assert not sent and not closes
    assert ui.pending_protections['entry']['protected_quantity'] == 2
    ui.trading_gateway.has_open_position = lambda symbol, position_side=None: False
    saved['result']['status'] = 'FILLED'
    MainWindow._restore_saved_protections(ui, {'records': [saved], 'errors': []})
    assert not sent and 'entry' in ui.submitted_protection_clients


def test_fail_safe_closes_remaining_tranche_once_even_if_both_legs_fail(qapp):
    from nightwatch.app.main_window import MainWindow
    ui, sent, closes, states = protection_ui()
    transmitted = []
    gateway = ui.trading_gateway
    gateway.remaining_protection_quantity = lambda context: Decimal('0.6')
    gateway.position_cache = {('BTCUSDT', 'BOTH'): {'positionAmt': '1'}}
    gateway.submit_order = lambda order, context: transmitted.append((order, context)) or 'close'
    gateway.request_was_admitted = lambda request: True
    ui._emergency_tranches = set()
    ui.emergency_reserved = {}
    ui.emergency_guards = {}
    ui.emergency_close_requests = {}
    ui.emergency_timer = SimpleNamespace(stop=lambda: None)
    ui._submit_emergency_close = lambda *a: MainWindow._submit_emergency_close(ui, *a)
    context = {'symbol': 'BTCUSDT', 'position_side': 'BOTH', 'entry_side': 'BUY',
               'quantity': '1', 'tranche_quantity': '1', 'entry_client_id': 'entry',
               'fill_total': 1, 'client_id': 'tp', 'rules': SymbolRules(market_step='0.1'), 'kind': 'tp'}
    MainWindow._start_emergency_close(ui, context, 'trigger rejected')
    MainWindow._start_emergency_close(ui, {**context, 'client_id': 'sl', 'kind': 'sl'}, 'trigger rejected')
    assert len(transmitted) == 1
    order, details = transmitted[0]
    assert order['quantity'] == '0.6' and order['side'] == 'SELL' and order['reduceOnly'] is True
    assert details['emergency_close'] and details['reserved'] == .6
    assert not ui.emergency_guards and ui.emergency_close_requests['close'] == details
