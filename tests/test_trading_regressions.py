from decimal import Decimal
import json

import pytest

from nightwatch.models import SymbolRules, quantize_step
from nightwatch.trading.orders import build_quick_order_request, protection_quantities
from nightwatch.trading.account_state import replay_account_events
from nightwatch.trading.gateway import _PlacementJournal, _journal_json


class Balance:
    def available_balance(self, asset):
        return 100


def preset(**changes):
    return dict(collateral_percent=100, leverage=1, order_mode='MARKET',
                slippage_enabled=True, max_slippage_percent='0.01', time_in_force='IOC', **changes)


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_slippage_cap_is_preserved_on_coarse_tick(side):
    rules = SymbolRules(tick_size='0.1', lot_step='0.001', min_notional=0)
    request = build_quick_order_request(Balance(), 'BTCUSDT', rules, side, preset(), 100.05, 100.05, 100.05, False)
    price = Decimal(request['order']['price'])
    bound = Decimal('100.05') * (1 + (Decimal('0.0001') if side == 'BUY' else -Decimal('0.0001')))
    assert price <= bound if side == 'BUY' else price >= bound
    assert Decimal(request['order']['quantity']) * max(price, Decimal('100.05')) <= 100


def test_buy_size_uses_worst_limit_price():
    p = preset()
    p['max_slippage_percent'] = 10
    request = build_quick_order_request(Balance(), 'BTCUSDT', SymbolRules(lot_step='0.001', tick_size='0.1'), 'BUY', p, 100, 100, 100, False)
    assert Decimal(request['order']['quantity']) * Decimal(request['order']['price']) <= 100
    assert request['collateral_required'] <= 100


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-1', '0'])
def test_quantizer_rejects_nonfinite_or_nonpositive(value):
    with pytest.raises(ValueError):
        quantize_step(value, '0.1')


def test_protection_allocation_conserves_quantized_total():
    quantities = protection_quantities(1.009, [{'percent': 25}, {'percent': 25}, {'percent': 50}], SymbolRules(market_step='0.01', min_market_qty=.01))
    assert sum(map(Decimal, quantities)) == Decimal('1')


def baseline():
    return {'account': {'availableBalance': '900', 'assets': [
        {'asset': 'USDT', 'availableBalance': '100', 'walletBalance': '100'},
        {'asset': 'USDC', 'availableBalance': '20', 'walletBalance': '20'}],
        'positions': [{'symbol': 'BTCUSDT', 'positionSide': 'BOTH', 'positionAmt': '1', 'leverage': 25}]},
        'ordersScope': 'ALL', 'orders': [{'symbol': 'BTCUSDT', 'clientOrderId': 'entry', 'status': 'NEW'}],
        'accountConfig': {'canTrade': True, 'multiAssetsMargin': False},
        'symbolConfig': [{'symbol': 'BTCUSDT', 'leverage': 25, 'marginType': 'CROSSED'}]}


def account_event():
    return {'e': 'ACCOUNT_UPDATE', 'a': {'B': [{'a': 'USDT', 'wb': '90', 'cw': '90'}],
                                      'P': [{'s': 'BTCUSDT', 'ps': 'BOTH', 'pa': '2', 'ep': '100', 'up': '4', 'mt': 'cross'}]}}


def test_replay_preserves_newer_positions_config_and_terminal_order():
    old = baseline()
    result = replay_account_events(old, [account_event(), {'e': 'ACCOUNT_CONFIG_UPDATE', 'ac': {'s': 'BTCUSDT', 'l': 5}},
                                               {'e': 'ORDER_TRADE_UPDATE', 'o': {'s': 'BTCUSDT', 'c': 'entry', 'X': 'FILLED', 'z': '2'}}])
    assert result['account']['positions'][0]['positionAmt'] == '2'
    assert result['account']['positions'][0]['leverage'] == 5
    assert result['symbolConfig'][0]['leverage'] == 5
    assert result['orders'] == []
    assert 'availableBalance' not in result['account']['assets'][0]
    assert old['account']['positions'][0]['positionAmt'] == '1'


def test_rest_refresh_publishes_reconciled_payload(gateway, monkeypatch):
    captured = []
    monkeypatch.setattr(gateway, '_launch_task', lambda f, done, failed, pool=None: captured.append((f, done, failed)) or object())
    delivered = []
    gateway.snapshot_ready.connect(delivered.append)
    gateway.refresh_account('BTCUSDT')
    gateway._cache_account_event(account_event())
    captured[0][1](baseline())
    assert gateway.position_cache[('BTCUSDT', 'BOTH')]['positionAmt'] == '2'
    assert delivered[0]['account']['positions'][0]['positionAmt'] == '2'


def test_asset_balance_and_local_reservations(gateway):
    gateway._cache_snapshot(baseline())
    assert gateway.available_balance('USDT') == 100
    assert gateway.available_balance('USDC') == 20
    gateway._collateral_reservations['entry'] = {'asset': 'USDC', 'amount': 12, 'accepted_at': None}
    assert gateway.available_balance('USDC') == 8
    assert gateway.available_balance('USDT') == 100
    gateway.multi_assets_margin = True
    with pytest.raises(ValueError, match='Single-Asset'):
        gateway.available_balance('USDT')


def test_latest_leverage_wins(gateway):
    gateway.position_cache[('BTCUSDT', 'BOTH')] = {'leverage': '25'}
    gateway.leverage_cache['BTCUSDT'] = 5
    assert gateway.current_leverage('BTCUSDT') == 5


def test_buffered_config_reduction_wins_over_rest_read(gateway, monkeypatch):
    captured = []
    monkeypatch.setattr(gateway, '_launch_task', lambda f, done, failed, pool=None: captured.append(done) or object())
    monkeypatch.setattr(gateway, '_queue_account_refresh', lambda *a: None)
    gateway.refresh_account('BTCUSDT')
    gateway._user_message(json.dumps({'e': 'ACCOUNT_CONFIG_UPDATE', 'ac': {'s': 'BTCUSDT', 'l': 5}}))
    captured[0](baseline())
    assert gateway.current_leverage('BTCUSDT') == 5
    assert gateway.position_cache[('BTCUSDT', 'BOTH')]['leverage'] == 5


def test_exchange_timestamp_keeps_newer_rest_position(gateway):
    snapshot = baseline()
    snapshot['account']['positions'][0]['updateTime'] = 2000
    event = {**account_event(), 'T': 1000}
    assert replay_account_events(snapshot, [event])['account']['positions'][0]['positionAmt'] == '1'
    gateway._cache_snapshot(snapshot)
    gateway._cache_account_event(event)
    assert gateway.position_cache[('BTCUSDT', 'BOTH')]['positionAmt'] == '1'
    gateway._cache_account_event({**event, 'T': 3000})
    assert gateway.position_cache[('BTCUSDT', 'BOTH')]['positionAmt'] == '2'


def test_second_order_cannot_reuse_reserved_collateral(gateway, monkeypatch):
    gateway._cache_snapshot(baseline())
    gateway.cross_ready.add('BTCUSDT')
    monkeypatch.setattr(gateway, '_send_or_rest', lambda request_id, *a: request_id)
    order = {'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': '1', 'newClientOrderId': 'one'}
    context = {'collateral_asset': 'USDT', 'collateral_required': 75}
    first = gateway.submit_order(order, context)
    assert first in gateway._collateral_reservations
    second = gateway.submit_order({**order, 'newClientOrderId': 'two'}, context)
    assert second not in gateway._collateral_reservations
    gateway._remember_failure(first, 'timeout', True, {'context': context})
    assert gateway.available_balance('USDT') == 25
    gateway._remember_failure(first, 'definite rejection', False, {'context': context})
    assert gateway.available_balance('USDT') == 100


def test_batch_members_count_against_entry_budget_separately_from_protection(gateway, monkeypatch):
    gateway.request_details = {
        'batch': {'method': 'batchOrders.place', 'expected_orders': [{}] * 5, 'context': {}},
        'entry': {'method': 'order.place', 'context': {}},
        'stop': {'method': 'algoOrder.place', 'context': {'protective_leg': True}},
    }
    assert gateway._open_order_write_count(protective=False) == 6
    assert gateway._open_order_write_count(protective=True) == 1
    gateway.cross_ready.add('BTCUSDT')
    monkeypatch.setattr(gateway, '_queue_placement', lambda *a: (_ for _ in ()).throw(AssertionError('Batch exceeds entry budget')))
    order = {'symbol': 'BTCUSDT', 'type': 'MARKET', 'side': 'BUY', 'quantity': '1'}
    request = gateway.submit_batch_orders([order] * 3, SymbolRules())
    assert request in gateway.terminal_requests


def test_accepted_entry_keeps_durable_protection_plan(tmp_path):
    journal = _PlacementJournal(str(tmp_path / 'intents.sqlite3'), 'mainnet:key')
    details = {'method': 'order.place', 'client_id': 'entry', 'fingerprint': 'fp',
               'expected': {'symbol': 'BTCUSDT', 'newClientOrderId': 'entry'},
               'context': {'protections': {'sl': [{'price': 90, 'percent': 100}]}, 'rules': SymbolRules()}}
    journal.run('put', 'request', _journal_json(details), 'fp')
    journal.run('delete', 'request')  # Accepted NEW response only resolves placement.
    loaded = journal.run('load')
    assert not loaded['placements']
    assert json.loads(loaded['protections'][0][1])['context']['protections']['sl']
    assert not _PlacementJournal(str(tmp_path / 'intents.sqlite3'), 'testnet:key').run('load')['protections']


def test_complete_tranche_survives_crash_before_first_leg(tmp_path):
    journal = _PlacementJournal(str(tmp_path / 'intents.sqlite3'), 'mainnet:key')
    records = [{'kind': 'entry', 'client_id': 'entry', 'context': {'protected_quantity': 2}},
               {'kind': 'leg', 'client_id': 'stop', 'context': {'entry_client_id': 'entry'}},
               {'kind': 'leg', 'client_id': 'tp', 'context': {'entry_client_id': 'entry'}}]
    journal.run('tranche', payload=_journal_json(records))
    assert {key for key, _payload in journal.run('load')['protections']} == {'entry', 'stop', 'tp'}


def test_journal_never_stores_credentials():
    encoded = _journal_json({'api_key': 'secret-key', 'nested': {'signature': 'sig', 'apiSecret': 'secret'}, 'rules': SymbolRules()})
    assert 'secret-key' not in encoded and 'sig' not in encoded and '"secret"' not in encoded


def test_protection_recovery_never_resends_missing_leg(gateway, monkeypatch):
    gateway.account_loaded = True
    gateway._protection_records = {'stop': {'kind': 'leg', 'client_id': 'stop', 'order': {'symbol': 'BTCUSDT'}, 'context': {}, 'method': 'algoOrder.place'}}
    monkeypatch.setattr(gateway.rest, 'query_order_by_client_id', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('Unknown order -2013')))
    captured = []
    monkeypatch.setattr(gateway, '_launch_task', lambda f, done, failed, pool=None: captured.append((f, done)) or object())
    monkeypatch.setattr(gateway, 'submit_order', lambda *a: pytest.fail('Recovery must not retransmit'))
    gateway._recover_protections()
    captured[0][1](captured[0][0]())
    assert gateway._protection_recovery_pending


def test_v3_account_fetches_explicit_configuration(monkeypatch):
    from nightwatch.networking.binance import BinanceRest
    monkeypatch.setattr(BinanceRest, '_ensure_time_sync_loop', lambda self: None)
    rest = BinanceRest()
    paths = []
    async def read(key, secret, path, params=None, **kwargs):
        paths.append(path)
        return {'/fapi/v3/account': {'assets': [], 'positions': [{'symbol': 'BTCUSDT', 'positionAmt': '1'}]},
                '/fapi/v3/positionRisk': [], '/fapi/v1/openOrders': [], '/fapi/v1/openAlgoOrders': [],
                '/fapi/v1/accountConfig': {'canTrade': True, 'multiAssetsMargin': False},
                '/fapi/v1/symbolConfig': [{'symbol': 'BTCUSDT', 'leverage': 5, 'marginType': 'CROSSED'}]}[path]
    async def mode(*a, **k):
        return {'dualSidePosition': False}
    monkeypatch.setattr(rest, '_signed_request_async', read)
    monkeypatch.setattr(rest, '_position_mode_async', mode)
    import asyncio
    monkeypatch.setattr('nightwatch.networking.binance.run_async', asyncio.run)
    result = rest.account_snapshot('key', 'secret')
    assert '/fapi/v1/accountConfig' in paths and '/fapi/v1/symbolConfig' in paths
    assert result['account']['positions'][0]['leverage'] == 5
    assert result['account']['canTrade'] is True
