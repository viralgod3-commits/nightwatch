from decimal import Decimal
import asyncio
import hashlib
import hmac
import json
import time
from unittest.mock import Mock
from urllib.parse import parse_qs, urlencode

import pytest

from nightwatch.models import Candle, SymbolRules, quantize_step
from nightwatch.trading.orders import RailAmendments, build_magnetic_rail_order_request, build_quick_order_request, build_smart_exit_orders, protection_quantities
from nightwatch.trading.account_state import normalize_order, replay_account_events, valid_order_fills
from nightwatch.trading.gateway import TradingGateway, _PlacementJournal, _journal_json
from nightwatch.networking.binance import BinanceRest, execution_outcome_uncertain


_RESTORE_PLACEMENT_JOURNAL = TradingGateway._restore_placement_journal


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
    monkeypatch.setattr(gateway, '_launch_task', lambda f, done, failed, pool=None: captured.append((f, done, failed)) or Mock())
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
    monkeypatch.setattr(gateway, '_launch_task', lambda f, done, failed, pool=None: captured.append(done) or Mock())
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
    monkeypatch.setattr(gateway, '_launch_task', lambda f, done, failed, pool=None: captured.append((f, done)) or Mock())
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


# Offline contract/fault tests: real application methods and SQLite, with only
# exchange responses and worker scheduling injected. These never place orders.
@pytest.fixture
def exchange(gateway, monkeypatch, tmp_path):
    gateway._journal = _PlacementJournal(str(tmp_path / 'intents.sqlite3'), 'testnet:test')
    gateway.cross_ready.add('BTCUSDT')
    monkeypatch.setattr(gateway, '_queue_account_refresh', lambda *a: None)
    jobs = []

    class Job:
        def __init__(self, function, done, failed):
            self.function, self.done, self.failed = function, done, failed
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

        def complete(self):
            assert not self.cancelled
            gateway.tasks.discard(self)
            try:
                result = self.function()
            except Exception as exc:
                self.failed(str(exc))
            else:
                self.done(result)

    def launch(function, done, failed, pool=None):
        job = Job(function, done, failed)
        jobs.append(job)
        return job

    monkeypatch.setattr(gateway, '_launch_task', launch)
    return gateway, jobs


def order_payload(kind='LIMIT', **changes):
    order = {'symbol': 'BTCUSDT', 'side': 'BUY', 'type': kind,
             'positionSide': 'BOTH', 'quantity': '1', 'newClientOrderId': 'entry'}
    if kind in {'LIMIT', 'STOP', 'TAKE_PROFIT'}:
        order.update(price='100', timeInForce='GTC')
    if kind in {'STOP', 'STOP_MARKET', 'TAKE_PROFIT', 'TAKE_PROFIT_MARKET'}:
        order.update(triggerPrice='110', workingType='MARK_PRICE', priceProtect=True)
    if kind == 'TRAILING_STOP_MARKET':
        order.update(activatePrice='90', callbackRate='0.5', workingType='MARK_PRICE')
    return {**order, **changes}


def accepted_order(order, **changes):
    algo = order['type'] not in {'LIMIT', 'MARKET'}
    row = {**order, 'origQty': order.get('quantity', '0'), 'executedQty': '0', 'avgPrice': '0'}
    row.pop('newClientOrderId', None)
    if algo:
        row.update(algoId=42, clientAlgoId=order.get('clientAlgoId') or order.get('newClientOrderId'), algoStatus='NEW')
    else:
        row.update(orderId=42, clientOrderId=order.get('newClientOrderId'), status='NEW')
    return {**row, **changes}


ORDER_KINDS = ['LIMIT', 'MARKET', 'STOP', 'STOP_MARKET', 'TAKE_PROFIT', 'TAKE_PROFIT_MARKET', 'TRAILING_STOP_MARKET']


@pytest.mark.parametrize('kind', ORDER_KINDS)
@pytest.mark.parametrize('position_side,side,intent', [
    ('BOTH', 'BUY', 'OPEN'), ('BOTH', 'SELL', 'REDUCE'),
    ('LONG', 'BUY', 'OPEN'), ('LONG', 'SELL', 'REDUCE'),
    ('SHORT', 'SELL', 'OPEN'), ('SHORT', 'BUY', 'REDUCE'),
])
def test_all_order_types_and_position_intents(kind, position_side, side, intent):
    order = order_payload(kind, positionSide=position_side, side=side)
    if position_side == 'BOTH' and intent == 'REDUCE':
        order['reduceOnly'] = True
    valid = TradingGateway._validate_order_payload(order, {'rules': SymbolRules(), 'position_intent': intent})
    assert Decimal(valid['quantity']) == 1
    assert valid['positionSide'] == position_side
    if position_side != 'BOTH':
        assert 'reduceOnly' not in valid


@pytest.mark.parametrize('kind', ['STOP_MARKET', 'TAKE_PROFIT_MARKET'])
@pytest.mark.parametrize('position_side,side', [('BOTH', 'BUY'), ('BOTH', 'SELL'), ('LONG', 'SELL'), ('SHORT', 'BUY')])
def test_close_all_excludes_quantity_and_reduce_only(kind, position_side, side):
    order = order_payload(kind, positionSide=position_side, side=side, closePosition='true')
    order.pop('quantity')
    valid = TradingGateway._validate_order_payload(order, {'rules': SymbolRules()})
    assert valid['closePosition'] is True
    assert 'quantity' not in valid and 'reduceOnly' not in valid


@pytest.mark.parametrize('changes', [
    {'quantity': 'NaN'}, {'quantity': 'Infinity'}, {'quantity': '0'}, {'quantity': '1.0001'},
    {'price': 'NaN'}, {'price': '100.001'}, {'side': 'INVALID'}, {'type': 'OCO'},
    {'symbol': ''}, {'reduceOnly': 'yes'}, {'positionSide': 'LONG', 'reduceOnly': True},
    {'closePosition': True}, {'priceMatch': 'QUEUE'}, {'timeInForce': 'INVALID'},
    {'newClientOrderId': 'illegal id'}, {'newClientOrderId': 'a' * 37},
])
def test_invalid_orders_are_rejected_before_any_write(exchange, changes):
    gateway, jobs = exchange
    failures = []
    gateway.request_failed.connect(lambda *args: failures.append(args))
    request = gateway.submit_order(order_payload(**changes), {'rules': SymbolRules()})
    assert request in gateway.terminal_requests
    assert not jobs and failures and failures[-1][2] is False


@pytest.mark.parametrize('kind', ORDER_KINDS)
def test_rest_dispatch_is_durable_and_uses_correct_order_service(exchange, monkeypatch, kind):
    gateway, jobs = exchange
    transmitted, succeeded = [], []
    gateway.request_succeeded.connect(lambda *args: succeeded.append(args))

    def send(key, secret, order, **kwargs):
        transmitted.append(order)
        return accepted_order(order)

    monkeypatch.setattr(gateway.rest, 'place_order', send)
    request = gateway.submit_order(order_payload(kind), {'rules': SymbolRules()})
    assert not transmitted
    jobs.pop(0).complete()  # Durable admission precedes transport.
    saved = gateway._journal.run('load')['placements']
    assert saved and not transmitted
    jobs.pop(0).complete()
    assert len(transmitted) == 1 and request in gateway.terminal_requests
    assert succeeded[0][1]['_transport'] == 'REST'
    if kind not in {'LIMIT', 'MARKET'}:
        assert transmitted[0]['algoType'] == 'CONDITIONAL'
        assert transmitted[0]['clientAlgoId'] == 'entry'
        assert 'newClientOrderId' not in transmitted[0]


@pytest.mark.parametrize('kind', ORDER_KINDS)
def test_rest_endpoint_and_parameter_contract(monkeypatch, kind):
    monkeypatch.setattr(BinanceRest, '_ensure_time_sync_loop', lambda self: None)
    rest, calls = BinanceRest(testnet=True), []
    monkeypatch.setattr(rest, 'signed_request', lambda *args, **kwargs: calls.append((args, kwargs)) or {})
    rest.place_order('key', 'secret', order_payload(kind))
    args, kwargs = calls[0]
    assert args[2] == ('/fapi/v1/order' if kind in {'LIMIT', 'MARKET'} else '/fapi/v1/algoOrder')
    assert args[4] == 'POST' and kwargs['order_count'] == 1
    assert 'stopPrice' not in args[3]


@pytest.mark.parametrize('method', ['GET', 'POST', 'PUT', 'DELETE'])
def test_signed_rest_bytes_match_hmac_and_flags(gateway, monkeypatch, method):
    calls = []
    monkeypatch.setattr(gateway.rest, 'has_fresh_time_offset', lambda *a: True)
    monkeypatch.setattr(gateway.rest, 'cached_timestamp_ms', lambda: 1770736694138)
    async def send(path, **kwargs):
        calls.append(kwargs)
        return {'code': 200}
    monkeypatch.setattr(gateway.rest, '_request_async', send)
    params = {'symbol': 'BTCUSDT', 'quantity': '0.123', 'reduceOnly': True, 'newClientOrderId': 'id:/x'}
    asyncio.run(gateway.rest._signed_request_async('key', 'secret', '/fapi/v1/order', params, method))
    call = calls[0]
    if method == 'GET':
        values = dict(call['params'])
        signature = values.pop('signature')
        unsigned = urlencode(values)
    else:
        unsigned, signature = call['body'].decode().rsplit('&signature=', 1)
        values = {key: row[0] for key, row in parse_qs(unsigned).items()}
    assert signature == hmac.new(b'secret', unsigned.encode(), hashlib.sha256).hexdigest()
    assert values['reduceOnly'] == 'true' and values['timestamp'] == '1770736694138'
    assert values['recvWindow'] == '5000' and call['headers']['X-MBX-APIKEY'] == 'key'
    assert params['reduceOnly'] is True and 'timestamp' not in params


def test_websocket_signature_uses_sorted_unescaped_values():
    params = {'timestamp': 1770736694138, 'newClientOrderId': 'id:/x', 'reduceOnly': True, 'quantity': '0.123'}
    raw = b'newClientOrderId=id:/x&quantity=0.123&reduceOnly=true&timestamp=1770736694138'
    assert TradingGateway._ws_signature(params, 'secret') == hmac.new(b'secret', raw, hashlib.sha256).hexdigest()


def test_failed_clock_sync_never_transmits(gateway, monkeypatch):
    monkeypatch.setattr(gateway.rest, 'has_fresh_time_offset', lambda *a: False)
    async def sync(**kwargs):
        raise RuntimeError('Network request timed out.')
    monkeypatch.setattr(gateway.rest, '_sync_time_once_async', sync)
    with pytest.raises(RuntimeError, match='Not sent:') as caught:
        asyncio.run(gateway.rest._signed_request_async('key', 'secret', '/fapi/v1/order', {}, 'POST'))
    assert not execution_outcome_uncertain(caught.value)


@pytest.mark.parametrize('error,uncertain', [
    ('HTTP 408: timeout', True), ('HTTP 500: internal server error', True),
    ('HTTP 503: Unknown error, please check your request or try again later.', True),
    ('HTTP 503: Service Unavailable.', False), ('HTTP 503: Internal error; unable to process your request.', False),
    ('HTTP 503 · Binance -1008: Request throttled', False), ('Binance -1007: Timeout waiting for response', True),
    ('HTTP 400 · Binance -2019: Margin is insufficient', False), ('Not sent: clock failed', False),
    ('Network request timed out.', True), ('Unexpected response: invalid JSON', True),
])
def test_exchange_error_outcome_classification(error, uncertain):
    assert execution_outcome_uncertain(error) is uncertain


@pytest.mark.parametrize('algo', [False, True])
def test_timeout_reconciles_by_client_id_without_resending(exchange, monkeypatch, algo):
    gateway, jobs = exchange
    calls = []
    expected = order_payload('STOP_MARKET' if algo else 'LIMIT')
    monkeypatch.setattr('nightwatch.trading.gateway.time.sleep', lambda *a: None)
    def place(*args, **kwargs):
        calls.append('write')
        raise RuntimeError('Network request timed out.')
    def query(*args, **kwargs):
        calls.append(('query', args[3], kwargs['algo']))
        return accepted_order(expected)
    monkeypatch.setattr(gateway.rest, 'place_order', place)
    monkeypatch.setattr(gateway.rest, 'query_order_by_client_id', query)
    request = gateway.submit_order(expected, {'rules': SymbolRules()})
    jobs.pop(0).complete()
    jobs.pop(0).complete()
    assert request in gateway.reconciling
    jobs.pop(0).complete()
    assert calls == ['write', ('query', 'entry', algo)]
    assert request in gateway.terminal_requests and not gateway.unresolved_fingerprints


def test_unknown_client_id_remains_blocked_and_durable(exchange, monkeypatch):
    gateway, jobs = exchange
    writes = []
    monkeypatch.setattr('nightwatch.trading.gateway.time.sleep', lambda *a: None)
    def place(*args, **kwargs):
        writes.append(1)
        raise RuntimeError('Network request timed out.')
    def query(*args, **kwargs):
        raise RuntimeError('HTTP 400 · Binance -2013: Order does not exist.')
    monkeypatch.setattr(gateway.rest, 'place_order', place)
    monkeypatch.setattr(gateway.rest, 'query_order_by_client_id', query)
    gateway.submit_order(order_payload(), {'rules': SymbolRules()})
    for _ in range(3):
        jobs.pop(0).complete()
    assert len(writes) == 1 and gateway.unresolved_fingerprints
    assert gateway._journal.run('load')['placements']
    gateway.submit_order(order_payload(newClientOrderId='duplicate'), {'rules': SymbolRules()})
    assert not jobs and len(writes) == 1


@pytest.mark.parametrize('conditional', [False, True])
def test_batch_success_tracks_explicit_exchange_client_ids(exchange, monkeypatch, conditional):
    gateway, jobs = exchange
    orders = [order_payload('STOP_MARKET' if conditional else 'LIMIT', newClientOrderId='one'),
              order_payload('TAKE_PROFIT_MARKET' if conditional else 'LIMIT', newClientOrderId='two', side='SELL')]
    if conditional:
        for order in orders:
            order['clientAlgoId'] = order.pop('newClientOrderId')
    monkeypatch.setattr(gateway.rest, 'place_batch_orders', lambda key, secret, orders: [accepted_order(order) for order in orders])
    request = gateway.submit_batch_orders(orders, SymbolRules())
    jobs.pop(0).complete()
    jobs.pop(0).complete()
    assert request in gateway.terminal_requests and request not in gateway.reconciling


def test_partial_batch_reports_acceptance_and_never_repeats_write(exchange, monkeypatch):
    gateway, jobs = exchange
    failures = []
    gateway.request_failed.connect(lambda *args: failures.append(args))
    orders = [order_payload(newClientOrderId='one'), order_payload(newClientOrderId='two')]
    monkeypatch.setattr(gateway.rest, 'place_batch_orders', lambda *a: [accepted_order(orders[0]), {'code': -2019, 'msg': 'Margin is insufficient'}])
    request = gateway.submit_batch_orders(orders, SymbolRules())
    jobs.pop(0).complete()
    jobs.pop(0).complete()
    assert request in gateway.terminal_requests
    assert '1 accepted and 1 failed' in failures[-1][1] and failures[-1][2] is False
    assert not gateway.unresolved_fingerprints


@pytest.mark.parametrize('kind', ['STOP_MARKET', 'TAKE_PROFIT_MARKET'])
def test_cancel_and_query_algo_use_only_documented_identifiers(gateway, monkeypatch, kind):
    calls = []
    response = accepted_order(order_payload(kind))
    monkeypatch.setattr(gateway.rest, 'signed_request', lambda *a, **k: calls.append(a) or response)
    gateway.rest.cancel_order('key', 'secret', {'symbol': 'BTCUSDT', 'algoId': 42}, algo=True)
    gateway.rest.query_order_by_client_id('key', 'secret', 'BTCUSDT', 'entry', algo=True)
    assert calls[0][3] == {'algoId': 42} and calls[0][4] == 'DELETE'
    assert calls[1][3] == {'clientAlgoId': 'entry'} and calls[1][4] == 'GET'


@pytest.mark.parametrize('status', ['NEW', 'CANCELED', 'FILLED'])
def test_query_rejects_wrong_order_identity(gateway, monkeypatch, status):
    monkeypatch.setattr(gateway.rest, 'signed_request', lambda *a, **k: {'orderId': 43, 'clientOrderId': 'other', 'symbol': 'BTCUSDT', 'status': status})
    with pytest.raises(RuntimeError, match='identity'):
        gateway.rest.query_order_request('key', 'secret', {'symbol': 'BTCUSDT', 'orderId': 42})


@pytest.mark.parametrize('changes', [
    {'origQty': '1', 'executedQty': '2'}, {'origQty': 'NaN'},
    {'executedQty': '-1'}, {'avgPrice': 'Infinity'},
])
def test_impossible_or_nonfinite_fills_require_reconciliation(changes):
    assert not valid_order_fills(changes)


def test_stale_protection_event_cannot_reopen_filled_leg(gateway, monkeypatch):
    monkeypatch.setattr(gateway, '_persist_protection_record', lambda *a: None)
    gateway._protection_records['stop'] = {'kind': 'leg', 'client_id': 'stop', 'context': {}}
    gateway._observe_protection_order('stop', {'status': 'FILLED', 'executedQty': '1', 'avgPrice': '100', 'updateTime': 2000})
    gateway._observe_protection_order('stop', {'status': 'NEW', 'executedQty': '0', 'avgPrice': '0', 'updateTime': 1000})
    record = gateway._protection_records['stop']
    assert record['last_status'] == 'FILLED'
    assert record['execution']['executedQty'] == '1' and record['execution']['avgPrice'] == '100'


@pytest.mark.parametrize('hedge,side', [(False, 'BUY'), (False, 'SELL'), (True, 'BUY'), (True, 'SELL')])
def test_quick_order_sizing_and_protection_directions(hedge, side):
    p = preset()
    p.update(take_profit_enabled=True, take_profit_percent=2, take_profit_close_percent=100,
             stop_loss_enabled=True, stop_loss_percent=1, stop_loss_close_percent=100)
    request = build_quick_order_request(Balance(), 'BTCUSDT', SymbolRules(), side, p, 100, 99.9, 100.1, hedge)
    valid = TradingGateway._validate_order_payload(request['order'], request)
    reference = Decimal('100.1' if side == 'BUY' else '99.9')
    tp, sl = (Decimal(str(request['protections'][kind][0]['price'])) for kind in ('tp', 'sl'))
    assert (sl < reference < tp) if side == 'BUY' else (tp < reference < sl)
    assert valid['positionSide'] == ('LONG' if side == 'BUY' else 'SHORT') if hedge else valid['positionSide'] == 'BOTH'
    assert request['collateral_required'] <= 100


@pytest.mark.parametrize('hedge,position_side,amount,side', [(False, 'BOTH', '0.29', 'SELL'), (False, 'BOTH', '-0.29', 'BUY'), (True, 'LONG', '0.29', 'SELL'), (True, 'SHORT', '-0.29', 'BUY')])
@pytest.mark.parametrize('role,kind', [('ENTRY', 'LIMIT'), ('TP', 'TAKE_PROFIT_MARKET'), ('SL', 'STOP_MARKET')])
def test_magnetic_rail_entry_and_reduction_sizing(hedge, position_side, amount, side, role, kind):
    rules = SymbolRules(lot_step='0.01', market_step='0.01')
    position = {'symbol': 'BTCUSDT', 'positionSide': position_side, 'positionAmt': amount}
    rail = {'side': side, 'railPrice': 100, 'sizePercent': 100, 'leverage': 5, 'orderRole': role, 'orderType': kind}
    request = build_magnetic_rail_order_request(Balance(), 'BTCUSDT', rules, rail, 100, 99, 101, hedge, [position])
    valid = TradingGateway._validate_order_payload(request['order'], request)
    if role == 'ENTRY':
        assert Decimal(valid['quantity']) == 5 and request['collateral_required'] <= 100
    else:
        assert Decimal(valid['quantity']) == Decimal('0.29') and request['collateral_required'] == 0
        assert valid.get('reduceOnly') is True if not hedge else 'reduceOnly' not in valid


@pytest.fixture
def socket_exchange(exchange, monkeypatch):
    from PySide6 import QtNetwork
    from nightwatch.trading.gateway import BINANCE_RATE_LIMITER
    gateway, jobs = exchange
    messages = []
    class Socket:
        def state(self):
            return QtNetwork.QAbstractSocket.SocketState.ConnectedState
        def sendTextMessage(self, message):
            messages.append(json.loads(message))
            return len(message)
        def close(self):
            pass
    gateway.trade_socket = Socket()
    gateway.trade_connected = True
    monkeypatch.setattr(gateway.rest, 'has_fresh_time_offset', lambda *a: True)
    monkeypatch.setattr(gateway.rest, 'cached_timestamp_ms', lambda: 1770736694138)
    monkeypatch.setattr(BINANCE_RATE_LIMITER, 'acquire', lambda *a, **k: None)
    return gateway, jobs, messages


@pytest.mark.parametrize('kind', ORDER_KINDS)
def test_websocket_order_wire_types_and_single_completion(socket_exchange, kind):
    gateway, jobs, messages = socket_exchange
    completed = []
    gateway.request_succeeded.connect(lambda *a: completed.append(a))
    request = gateway.submit_order(order_payload(kind), {'rules': SymbolRules()})
    jobs.pop(0).complete()
    assert len(messages) == 1 and request in gateway.pending
    wire = messages[0]
    signed = dict(wire['params'])
    signature = signed.pop('signature')
    assert signature == gateway._ws_signature(signed, 'test-secret')
    assert isinstance(signed['timestamp'], int) and isinstance(signed['recvWindow'], int)
    assert signed['quantity'] == '1' and wire['id'] == request
    assert wire['method'] == ('order.place' if kind in {'LIMIT', 'MARKET'} else 'algoOrder.place')
    if kind not in {'LIMIT', 'MARKET'}:
        assert signed['clientAlgoId'] == 'entry' and 'newClientOrderId' not in signed
        if kind != 'TRAILING_STOP_MARKET':
            assert signed['priceProtect'] == 'true'
    ack = json.dumps({'id': request, 'status': 200, 'result': accepted_order(order_payload(kind))})
    gateway._trade_message(ack)
    gateway._trade_message(ack)  # Duplicate acknowledgement cannot complete twice.
    assert len(completed) == 1 and request not in gateway.pending


@pytest.mark.parametrize('kind,tif', [('STOP', 'GTX'), ('TAKE_PROFIT', 'GTD')])
def test_conditional_tif_outside_ws_schema_uses_rest_once(socket_exchange, monkeypatch, kind, tif):
    gateway, jobs, messages = socket_exchange
    calls = []
    order = order_payload(kind, timeInForce=tif)
    if tif == 'GTD':
        order['goodTillDate'] = 1770737395000
    monkeypatch.setattr(gateway.rest, 'place_order', lambda key, secret, order, **k: calls.append(order) or accepted_order(order))
    request = gateway.submit_order(order, {'rules': SymbolRules()})
    jobs.pop(0).complete()
    assert not messages
    jobs.pop(0).complete()
    assert len(calls) == 1 and request in gateway.terminal_requests


def test_socket_write_failure_never_falls_back_to_second_write(socket_exchange, monkeypatch):
    gateway, jobs, messages = socket_exchange
    def broken_write(message):
        raise OSError('broken pipe')
    monkeypatch.setattr(gateway.trade_socket, 'sendTextMessage', broken_write)
    monkeypatch.setattr(gateway.rest, 'place_order', lambda *a, **k: pytest.fail('Uncertain socket write must not be repeated over REST'))
    gateway.submit_order(order_payload(), {'rules': SymbolRules()})
    jobs.pop(0).complete()
    assert not messages and gateway.reconciling
    assert len(jobs) == 1  # A read-only reconciliation job.


def test_account_stream_can_confirm_before_trade_ack(socket_exchange):
    gateway, jobs, messages = socket_exchange
    completed = []
    gateway.request_succeeded.connect(lambda *a: completed.append(a))
    request = gateway.submit_order(order_payload(), {'rules': SymbolRules()})
    jobs.pop(0).complete()
    gateway._user_message(json.dumps({'e': 'ORDER_TRADE_UPDATE', 'T': 2000,
        'o': {'s': 'BTCUSDT', 'c': 'entry', 'i': 42, 'S': 'BUY', 'o': 'LIMIT', 'ps': 'BOTH',
              'X': 'FILLED', 'q': '1', 'z': '1', 'p': '100', 'ap': '100', 'f': 'GTC'}}))
    gateway._trade_message(json.dumps({'id': request, 'status': 200, 'result': accepted_order(order_payload())}))
    assert len(completed) == 1 and completed[0][1]['status'] == 'FILLED'
    assert request not in gateway.pending and not gateway.unresolved_fingerprints


@pytest.mark.parametrize('algo', [False, True])
def test_cancel_is_available_while_disarmed_and_sends_integer_ids(socket_exchange, algo):
    gateway, jobs, messages = socket_exchange
    request = gateway.submit_cancel({'symbol': 'BTCUSDT', 'algoId' if algo else 'orderId': '42'}, algo)
    wire = messages[0]
    assert wire['method'] == ('algoOrder.cancel' if algo else 'order.cancel')
    assert wire['params']['algoId' if algo else 'orderId'] == 42
    if algo:
        assert 'symbol' not in wire['params']
    response = {'algoId': 42, 'clientAlgoId': 'entry', 'code': '200', 'msg': 'success'} if algo else accepted_order(order_payload(), status='CANCELED')
    gateway._trade_message(json.dumps({'id': request, 'status': 200, 'result': response}))
    assert request in gateway.terminal_requests and not gateway.armed


def test_modify_keeps_reduce_only_and_total_filled_quantity(socket_exchange):
    gateway, jobs, messages = socket_exchange
    request = gateway.submit_modify({'symbol': 'BTCUSDT', 'side': 'SELL', 'orderId': '42',
        'price': '100', 'quantity': '1', 'reduceOnly': 'true', '_minimumExecutedQty': '0.3'}, SymbolRules())
    signed = messages[0]['params']
    assert signed['reduceOnly'] == 'true' and signed['orderId'] == 42
    assert '_minimumExecutedQty' not in signed and signed['quantity'] == '1'
    gateway._trade_message(json.dumps({'id': request, 'status': 200,
        'result': accepted_order(order_payload(side='SELL', reduceOnly=True), executedQty='0.3')}))
    assert request in gateway.terminal_requests


@pytest.mark.parametrize('kind', ['modify', 'cancel'])
def test_uncertain_amendment_or_cancel_queries_without_repeating(exchange, monkeypatch, kind):
    gateway, jobs = exchange
    calls = []
    changes = {'symbol': 'BTCUSDT', 'orderId': 42, 'side': 'BUY', 'quantity': '1', 'price': '100'}
    def write(*a, **k):
        calls.append('write')
        raise RuntimeError('Network request timed out.')
    def query(*a, **k):
        calls.append('query')
        return accepted_order(order_payload(), status='NEW' if kind == 'modify' else 'CANCELED')
    monkeypatch.setattr(gateway.rest, 'modify_order' if kind == 'modify' else 'cancel_order', write)
    monkeypatch.setattr(gateway.rest, 'query_order_request', query)
    request = gateway.submit_modify(changes, SymbolRules()) if kind == 'modify' else gateway.submit_cancel({'symbol': 'BTCUSDT', 'orderId': 42})
    jobs.pop(0).complete()
    jobs.pop(0).complete()
    assert calls == ['write', 'query'] and request in gateway.terminal_requests


@pytest.mark.parametrize('deadline,valid', [(1770737295999, True), (1770737294999, False), (1770737294000, False), (1770737293999, False)])
def test_gtd_enforces_deadline_after_seconds_truncation(deadline, valid):
    order = order_payload(timeInForce='GTD', goodTillDate=deadline)
    context = {'rules': SymbolRules(), '_server_time_ms': 1770736694138}
    if valid:
        assert TradingGateway._validate_order_payload(order, context)['goodTillDate'] % 1000 == 0
    else:
        with pytest.raises(ValueError, match='600 seconds'):
            TradingGateway._validate_order_payload(order, context)


@pytest.mark.parametrize('callback,valid', [('0.1', True), ('10', True), ('0.09', False), ('10.1', False), ('NaN', False)])
def test_trailing_callback_bounds(callback, valid):
    order = order_payload('TRAILING_STOP_MARKET', callbackRate=callback)
    if valid:
        TradingGateway._validate_order_payload(order, {'rules': SymbolRules()})
    else:
        with pytest.raises(ValueError, match='callback'):
            TradingGateway._validate_order_payload(order, {'rules': SymbolRules()})


def test_market_lot_grid_must_also_satisfy_regular_lot_size():
    rules = SymbolRules(lot_step='0.01', market_step='0.001')
    with pytest.raises(ValueError, match='increments'):
        TradingGateway._validate_order_payload(order_payload('MARKET', quantity='0.011'), {'rules': rules})


@pytest.mark.parametrize('position_side,amount', [('BOTH', '0.29'), ('BOTH', '-0.29'), ('LONG', '0.29'), ('SHORT', '-0.29')])
def test_smart_exit_is_passive_and_conserves_unreserved_quantity(position_side, amount):
    candles = [Candle(i, 100, 101 + (i % 7) * .1, 99 - (i % 7) * .1, 100, 1, 100) for i in range(40)]
    rules = SymbolRules(tick_size='0.01', lot_step='0.01', min_qty=.01)
    position = {'symbol': 'BTCUSDT', 'positionSide': position_side, 'positionAmt': amount}
    orders = build_smart_exit_orders(position, candles, rules, 99.9, 100.1, reserved_quantity=.09)
    assert 1 <= len(orders) <= 3
    assert sum(Decimal(o['quantity']) for o in orders) == Decimal('0.20')
    for order in orders:
        TradingGateway._validate_order_payload(order, {'rules': rules, 'position_intent': 'REDUCE'})
        assert order['timeInForce'] == 'GTX'
        assert Decimal(order['price']) > Decimal('99.9') if order['side'] == 'SELL' else Decimal(order['price']) < Decimal('100.1')


def test_close_all_execution_is_not_treated_as_fixed_quantity_overfill():
    assert valid_order_fills({'origQty': '0', 'executedQty': '2', 'closePosition': True})


def test_finished_algo_uses_verified_child_fill_status(gateway, monkeypatch):
    parent = {'algoId': 42, 'actualOrderId': 100, 'algoStatus': 'FINISHED', 'quantity': '1', 'actualQty': '0', 'actualPrice': '0'}
    monkeypatch.setattr(gateway.rest, 'query_order', lambda *a: {'orderId': 100, 'status': 'PARTIALLY_FILLED', 'executedQty': '0.4', 'avgPrice': '110'})
    result = gateway._with_child_execution('key', 'secret', 'BTCUSDT', parent)
    assert result['status'] == 'PARTIALLY_FILLED' and result['executedQty'] == '0.4'
    assert normalize_order(result)['executedQty'] == '0.4' and result['_child_reconciled']


def test_cancel_all_attempts_both_services_even_when_one_rejects(gateway, monkeypatch):
    paths = []
    def signed(key, secret, path, *a, **k):
        paths.append(path)
        if path == '/fapi/v1/allOpenOrders':
            raise RuntimeError('HTTP 400 · Binance -2015: Invalid API key')
        return {'code': 200, 'msg': 'success'}
    monkeypatch.setattr(gateway.rest, 'signed_request', signed)
    result = gateway.rest.cancel_all_orders('key', 'secret', 'BTCUSDT')
    assert paths == ['/fapi/v1/allOpenOrders', '/fapi/v1/algoOpenOrders']
    assert result['standard']['uncertain'] is False and result['algo']['code'] == 200


def test_malformed_cancel_all_reply_is_not_reported_as_success(gateway, monkeypatch):
    monkeypatch.setattr(gateway.rest, 'signed_request', lambda *a, **k: {})
    result = gateway.rest.cancel_all_orders('key', 'secret', 'BTCUSDT')
    assert result['standard']['uncertain'] and result['algo']['uncertain']


@pytest.mark.parametrize('reply', [{}, {'symbol': 'ETHUSDT', 'leverage': 5}, {'symbol': 'BTCUSDT', 'leverage': 0}])
def test_leverage_needs_verified_symbol_and_value(gateway, monkeypatch, reply):
    monkeypatch.setattr(gateway.rest, 'signed_request', lambda *a, **k: reply)
    with pytest.raises(RuntimeError, match='Unexpected response'):
        gateway.rest.change_leverage('key', 'secret', 'BTCUSDT', 5)


def test_cross_margin_accepts_only_success_or_already_cross(gateway, monkeypatch):
    monkeypatch.setattr(gateway.rest, 'signed_request', lambda *a, **k: {})
    with pytest.raises(RuntimeError, match='Unexpected response'):
        gateway.rest.ensure_cross_margin('key', 'secret', 'BTCUSDT')
    def already_cross(*a, **k):
        raise RuntimeError('HTTP 400 · Binance -4046: No need to change margin type.')
    monkeypatch.setattr(gateway.rest, 'signed_request', already_cross)
    assert gateway.rest.ensure_cross_margin('key', 'secret', 'BTCUSDT')['unchanged']


@pytest.mark.parametrize('response', [{'symbol': 'BTCUSDT', 'leverage': 5}, {'symbol': 'BTCUSDT', 'leverage': '5'}])
def test_valid_leverage_reply_is_accepted(gateway, monkeypatch, response):
    monkeypatch.setattr(gateway.rest, 'signed_request', lambda *a, **k: response)
    assert gateway.rest.change_leverage('key', 'secret', 'BTCUSDT', 5) == response


def test_journal_write_failure_prevents_order_transmission(exchange, monkeypatch):
    gateway, jobs = exchange
    def disk_failure(*a, **k):
        raise OSError('Disk full')
    monkeypatch.setattr(gateway._journal, 'run', disk_failure)
    monkeypatch.setattr(gateway.rest, 'place_order', lambda *a, **k: pytest.fail('Unjournaled order was sent'))
    request = gateway.submit_order(order_payload(), {'rules': SymbolRules()})
    jobs.pop(0).complete()
    assert request in gateway.terminal_requests and gateway._journal_error
    assert not gateway.request_was_admitted(request)


def test_shutdown_keeps_durable_order_for_restart_and_cancels_workers(exchange, monkeypatch):
    gateway, jobs = exchange
    writes = []
    monkeypatch.setattr(gateway.rest, 'place_order', lambda *a, **k: writes.append(a))
    request = gateway.submit_order(order_payload(), {'rules': SymbolRules()})
    jobs.pop(0).complete()
    gateway.stop()
    assert jobs[0].cancelled and not writes
    assert gateway._journal.run('load')['placements'][0][0] == request
    assert gateway.unresolved_fingerprints and not gateway.armed


def test_credentials_cannot_change_during_unknown_cancel(gateway):
    gateway.request_details['cancel'] = {'method': 'order.cancel', 'transport_state': 'uncertain'}
    gateway.set_credentials('other-key', 'other-secret')
    assert gateway.api_key == 'test-key' and gateway.api_secret == 'test-secret'


@pytest.mark.parametrize('hedge,position_side', [(True, 'BOTH'), (False, 'LONG'), (False, 'SHORT')])
def test_account_position_mode_mismatch_prevents_any_write(exchange, hedge, position_side):
    gateway, jobs = exchange
    gateway.hedge_mode = hedge
    request = gateway.submit_order(order_payload(positionSide=position_side), {'rules': SymbolRules()})
    assert request in gateway.terminal_requests and not jobs


def test_filled_tranche_cancels_sibling_and_bounds_fail_safe(gateway, monkeypatch):
    cancels = []
    monkeypatch.setattr(gateway, '_persist_protection_record', lambda *a: None)
    monkeypatch.setattr(gateway, 'submit_cancel', lambda *a, **k: cancels.append((a, k)))
    context = {'entry_client_id': 'entry', 'fill_total': 1, 'tranche_quantity': '1'}
    gateway._protection_records = {
        'tp': {'kind': 'leg', 'client_id': 'tp', 'context': context, 'last_status': 'NEW', 'order': {'symbol': 'BTCUSDT'}, 'execution': {'algoId': 41}},
        'sl': {'kind': 'leg', 'client_id': 'sl', 'context': context, 'last_status': 'NEW', 'order': {'symbol': 'BTCUSDT'}, 'execution': {'algoId': 42}},
    }
    gateway._observe_protection_order('tp', {'algoId': 41, 'status': 'PARTIALLY_FILLED', 'executedQty': '0.4'})
    assert gateway.remaining_protection_quantity(context) == Decimal('0.6') and not cancels
    gateway._observe_protection_order('tp', {'algoId': 41, 'status': 'FILLED', 'executedQty': '1'})
    assert gateway.remaining_protection_quantity(context) == 0
    assert cancels[0][0] == ({'symbol': 'BTCUSDT', 'clientAlgoId': 'sl'}, True)
    assert 'sl' in gateway._protection_cleanup_clients


def test_obsolete_queued_protection_cannot_be_transmitted(exchange, monkeypatch):
    gateway, jobs = exchange
    written = []
    def place(key, secret, order, *, before_send):
        before_send()
        written.append(order)
        return accepted_order(order)
    monkeypatch.setattr(gateway.rest, 'place_order', place)
    order = order_payload('STOP_MARKET', side='SELL', reduceOnly=True)
    context = {'rules': SymbolRules(), 'protective_leg': True, 'client_id': 'entry', 'quantity': '1'}
    request = gateway.submit_order(order, context)
    jobs.pop(0).complete()
    gateway._protection_cancel_tokens['entry'].set()  # Another exit filled before worker transmission.
    jobs.pop(0).complete()
    assert not written and request in gateway.terminal_requests
    assert gateway.failure_context(request)['protection_obsolete']
    assert gateway._protection_records['entry']['last_status'] == 'CANCELED'


def test_hedge_recovery_retires_closed_long_independently_of_open_short(exchange):
    gateway, jobs = exchange
    gateway.position_cache[('BTCUSDT', 'SHORT')] = {'positionAmt': '-2'}
    gateway._protection_records['entry'] = {'kind': 'entry', 'client_id': 'entry',
        'context': {}, 'last_status': 'FILLED', 'order': {'symbol': 'BTCUSDT', 'positionSide': 'LONG'}}
    gateway._retire_closed_protections({'ordersScope': 'ALL', 'orders': [], 'algoOrders': [], '_read_started_mono': time.monotonic()})
    assert not gateway._protection_records
    assert gateway.has_open_position('BTCUSDT', 'SHORT')


def test_invalid_secondary_algo_client_id_is_rejected():
    with pytest.raises(ValueError, match='Client order ID'):
        TradingGateway._validate_order_payload(order_payload('STOP_MARKET', clientAlgoId='illegal id'), {'rules': SymbolRules()})


def test_verified_child_can_replace_finished_parent_with_working_status(gateway, monkeypatch):
    monkeypatch.setattr(gateway, '_persist_protection_record', lambda *a: None)
    gateway._protection_records['stop'] = {'kind': 'leg', 'client_id': 'stop', 'context': {},
        'last_status': 'FINISHED', 'execution': {'actualOrderId': 100, 'executedQty': '0'}}
    gateway._observe_protection_order('stop', {'orderId': 100, 'status': 'PARTIALLY_FILLED', 'executedQty': '0.4', 'avgPrice': '100'})
    assert gateway._protection_records['stop']['last_status'] == 'PARTIALLY_FILLED'


def test_local_rate_limit_prevents_both_socket_and_rest_writes(socket_exchange, monkeypatch):
    from nightwatch.trading.gateway import BINANCE_RATE_LIMITER, LocalRateLimitError
    gateway, jobs, messages = socket_exchange
    def limited(*a, **k):
        raise LocalRateLimitError('Published rate limit reached')
    monkeypatch.setattr(BINANCE_RATE_LIMITER, 'acquire', limited)
    monkeypatch.setattr(gateway.rest, 'place_order', lambda *a, **k: pytest.fail('Rate-limited order was sent'))
    request = gateway.submit_order(order_payload(), {'rules': SymbolRules()})
    jobs.pop(0).complete()
    assert request in gateway.terminal_requests and not messages
    assert not gateway.unresolved_fingerprints


def test_definitive_socket_rejection_does_not_reconcile_or_retry(socket_exchange):
    gateway, jobs, messages = socket_exchange
    failures = []
    gateway.request_failed.connect(lambda *a: failures.append(a))
    request = gateway.submit_order(order_payload(), {'rules': SymbolRules()})
    jobs.pop(0).complete()
    gateway._trade_message(json.dumps({'id': request, 'status': 400, 'error': {'code': -2019, 'msg': 'Margin is insufficient'}}))
    assert len(messages) == 1 and request in gateway.terminal_requests
    assert not gateway.reconciling and not gateway.unresolved_fingerprints and failures[-1][2] is False


def test_batch_serializes_nested_flags_as_exchange_strings(gateway, monkeypatch):
    calls = []
    monkeypatch.setattr(gateway.rest, 'signed_request', lambda *a, **k: calls.append((a, k)) or [])
    gateway.rest.place_batch_orders('key', 'secret', [order_payload(reduceOnly=True)])
    args, kwargs = calls[0]
    assert args[2] == '/fapi/v1/batchOrders' and args[4] == 'POST'
    assert args[3]['batchOrders'][0]['reduceOnly'] == 'true'
    assert args[3]['batchOrders'][0]['quantity'] == '1' and kwargs['order_count'] == 5


def test_conditional_batch_stops_after_failure_and_marks_unsent_members(gateway, monkeypatch):
    writes = []
    def send(key, secret, order):
        writes.append(order['newClientOrderId'])
        if order['newClientOrderId'] == 'two':
            raise RuntimeError('HTTP 400 · Binance -2019: Margin is insufficient')
        return accepted_order(order)
    monkeypatch.setattr(gateway.rest, 'place_order', send)
    orders = [order_payload('STOP_MARKET', newClientOrderId=client) for client in ('one', 'two', 'three')]
    result = gateway.rest.place_batch_orders('key', 'secret', orders)
    assert writes == ['one', 'two']
    assert result[1]['code'] == -2019 and result[1]['_uncertain'] is False
    assert result[2]['_notSent']


def test_cross_and_leverage_confirm_from_exchange_with_latest_setting_queued(exchange, monkeypatch, qapp):
    gateway, jobs = exchange
    gateway.cross_ready.clear()
    calls = []
    def signed(key, secret, path, params, method):
        calls.append((path, params))
        return {'code': 200} if path.endswith('marginType') else {'symbol': params['symbol'], 'leverage': params['leverage']}
    monkeypatch.setattr(gateway.rest, 'signed_request', signed)
    gateway.apply_cross_leverage('BTCUSDT', 5)
    gateway.apply_cross_leverage('BTCUSDT', 10)
    jobs.pop(0).complete()
    qapp.processEvents()
    jobs.pop(0).complete()
    assert [params.get('leverage') for path, params in calls if path.endswith('leverage')] == [5, 10]
    assert sum(path.endswith('marginType') for path, params in calls) == 1
    assert gateway.current_leverage('BTCUSDT') == 10 and not gateway.cross_pending


def test_unknown_setting_reply_does_not_confirm_cross_ready(exchange, monkeypatch):
    gateway, jobs = exchange
    gateway.cross_ready.clear()
    monkeypatch.setattr(gateway.rest, 'signed_request', lambda *a, **k: {})
    gateway.apply_cross_leverage('BTCUSDT', 5)
    jobs.pop(0).complete()
    assert 'BTCUSDT' not in gateway.cross_ready and gateway.current_leverage('BTCUSDT') == 0


def test_user_stream_start_keepalive_rotation_and_expiry(exchange, monkeypatch):
    gateway, jobs = exchange
    opened, replaced = [], []
    monkeypatch.setattr(gateway, '_open_user_socket', opened.append)
    monkeypatch.setattr(gateway, '_replace_user_stream', lambda: replaced.append(True))
    monkeypatch.setattr(gateway.rest, 'start_user_stream', lambda key: 'first')
    monkeypatch.setattr(gateway.rest, 'keepalive_user_stream', lambda *a: {'listenKey': 'second'})
    gateway._start_user_stream()
    jobs.pop(0).complete()
    assert gateway.listen_key == 'first' and opened == ['first']
    gateway._keepalive_user_stream()
    jobs.pop(0).complete()
    assert gateway.listen_key == 'second' and opened == ['first', 'second']
    gateway._user_message(json.dumps({'e': 'listenKeyExpired'}))
    assert replaced == [True]


@pytest.mark.parametrize('price_match', ['OPPONENT', 'OPPONENT_5', 'OPPONENT_10', 'OPPONENT_20', 'QUEUE', 'QUEUE_5', 'QUEUE_10', 'QUEUE_20'])
@pytest.mark.parametrize('kind', ['LIMIT', 'STOP', 'TAKE_PROFIT'])
def test_supported_price_match_orders_have_no_explicit_limit_price(price_match, kind):
    order = order_payload(kind, priceMatch=price_match)
    order.pop('price')
    valid = TradingGateway._validate_order_payload(order, {'rules': SymbolRules()})
    assert valid['priceMatch'] == price_match and 'price' not in valid


@pytest.mark.parametrize('time_in_force', ['GTC', 'IOC', 'FOK', 'GTX', 'RPI'])
def test_supported_standard_limit_time_in_force(time_in_force):
    order = order_payload(timeInForce=time_in_force)
    assert TradingGateway._validate_order_payload(order, {'rules': SymbolRules()})['timeInForce'] == time_in_force


@pytest.mark.parametrize('child_id,status,replace', [(None, 'CANCELED', True), (100, 'CANCELED', False), (100, 'FINISHED', False), (None, 'NEW', False)])
def test_conditional_rail_replacement_requires_canceled_parent_without_child(exchange, monkeypatch, qapp, child_id, status, replace):
    from PySide6 import QtWidgets
    gateway, jobs = exchange
    owner = QtWidgets.QWidget()
    owner.trading_gateway = gateway
    owner.active_protection_legs = {}
    owner.pending_protections = {}
    owner.statusBar = lambda: Mock()
    amendments = RailAmendments(owner)
    item = {'cancel_request': {'symbol': 'BTCUSDT', 'algoId': 42}, 'cancel_algo': True,
            'request': order_payload('STOP_MARKET', newClientOrderId='replacement'),
            'context': {'rules': SymbolRules()}, 'protection': {}, 'old_client': 'entry',
            'identity': ('BTCUSDT', 'ALGO', '42'), 'stage': 'cancel', 'draft': 0, 'chart': None}
    row = {'symbol': 'BTCUSDT', 'algoId': 42, 'algoStatus': status, 'quantity': '1', 'actualQty': '0.2'}
    if child_id:
        row['actualOrderId'] = child_id
    monkeypatch.setattr(gateway.rest, 'query_order_request', lambda *a, **k: row)
    replacements = []
    monkeypatch.setattr(gateway, 'submit_order', lambda order, context: replacements.append(order) or 'replacement-request')
    monkeypatch.setattr(gateway, 'request_was_admitted', lambda request: True)
    monkeypatch.setattr(gateway, 'refresh_account', lambda *a, **k: None)
    try:
        amendments._confirm_cancelled(item)
        jobs.pop(0).complete()
        assert bool(replacements) is replace
        if replace:
            assert replacements[0]['quantity'] == '0.8'
    finally:
        owner.close()


def test_stream_envelope_timestamp_rejects_stale_terminal_protection_event(gateway, monkeypatch):
    monkeypatch.setattr(gateway, '_persist_protection_record', lambda *a: None)
    gateway._protection_records['stop'] = {'kind': 'leg', 'client_id': 'stop', 'context': {},
        'last_status': 'CANCELED', 'execution': {'executedQty': '0', 'updateTime': 2000}}
    gateway._cache_account_event({'e': 'ALGO_UPDATE', 'T': 1000,
        'o': {'caid': 'stop', 'aid': 42, 'X': 'FINISHED', 'aq': '0', 'ap': '0', 's': 'BTCUSDT'}})
    assert gateway._protection_records['stop']['last_status'] == 'CANCELED'


def test_restart_restores_durable_intent_and_only_queries_existing_order(exchange, monkeypatch, tmp_path):
    from PySide6 import QtCore
    gateway, jobs = exchange
    order = order_payload()
    fingerprint = gateway._order_fingerprint(order)
    scope = 'mainnet:' + hashlib.sha256(gateway.api_key.encode()).hexdigest()
    journal = _PlacementJournal(str(tmp_path / 'unresolved_placements.sqlite3'), scope)
    journal.run('put', 'crashed-request', _journal_json({'method': 'order.place', 'expected': order,
        'client_id': 'entry', 'fingerprint': fingerprint, 'context': {'rules': SymbolRules()}}), fingerprint)
    monkeypatch.setattr(QtCore.QStandardPaths, 'writableLocation', lambda *a: str(tmp_path))
    monkeypatch.setattr(gateway.rest, 'query_order_by_client_id', lambda *a, **k: accepted_order(order))
    monkeypatch.setattr(gateway.rest, 'place_order', lambda *a, **k: pytest.fail('Restart must never retransmit a durable intent'))
    gateway._journal = None
    _RESTORE_PLACEMENT_JOURNAL(gateway)
    jobs.pop(0).complete()
    assert gateway.unresolved_fingerprints and gateway.reconciling
    jobs.pop(0).complete()
    jobs.pop(0).complete()  # Delete only after the client-ID query confirms it.
    assert 'crashed-request' in gateway.terminal_requests and not gateway.unresolved_fingerprints
    assert not journal.run('load')['placements']


@pytest.mark.parametrize('status,body,uncertain', [
    (400, {'code': -2019, 'msg': 'Margin is insufficient'}, False),
    (503, {'msg': 'Service Unavailable.'}, False),
    (503, {'msg': 'Unknown error, please check your request or try again later.'}, True),
    (408, {'msg': 'Request timeout'}, True),
    (200, 'invalid JSON', True),
])
def test_http_transport_reports_binance_rejection_and_unknown_outcomes(status, body, uncertain):
    import httpx
    from nightwatch.networking.binance import _AsyncHttpRuntime
    async def request():
        runtime = _AsyncHttpRuntime()
        def reply(req):
            return httpx.Response(status, json=body) if isinstance(body, dict) else httpx.Response(status, text=body)
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            runtime._client = client
            return await runtime.request_json('https://demo-fapi.binance.com/fapi/v1/order', method='POST')
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(request())
    assert execution_outcome_uncertain(caught.value) is uncertain
