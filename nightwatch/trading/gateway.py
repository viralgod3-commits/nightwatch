"""Binance execution adapter and account lifecycle."""
from __future__ import annotations


import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
from collections import deque
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from typing import Any

from PySide6 import QtCore, QtNetwork
from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtWebSockets import QWebSocket

from ..networking.binance import BinanceRest
from ..constants import (
    CONDITIONAL_ORDER_TYPES,
    MAIN_PRIVATE_WS,
    MAIN_TRADE_WS,
    STANDARD_ORDER_TYPES,
    TEST_PRIVATE_WS,
    TEST_TRADE_WS,
)
from ..models import SymbolRules
from ..networking.binance import ApiTask, launch_task
from ..networking.binance import (
    BINANCE_RATE_LIMITER, LocalRateLimitError, binance_error_code, execution_outcome_uncertain,
)
from ..models import safe_float, validate_step
from ..models import DiagnosticsPort
from .account_state import event_timestamp, exchange_bool, normalize_order, older_than_row, replay_account_events, valid_order_fills


def _journal_json(value) -> str:
    def sanitize(item):
        if isinstance(item, SymbolRules):
            return sanitize(asdict(item))
        if isinstance(item, dict):
            return {key: sanitize(value) for key, value in item.items()
                    if str(key).replace('_', '').casefold() not in {'apikey', 'apisecret', 'secret', 'signature'}
                    and not (key in {'max_price', 'max_qty', 'max_market_qty'} and value == float('inf'))}
        if isinstance(item, (list, tuple)):
            return [sanitize(value) for value in item]
        return item
    return json.dumps(sanitize(value), separators=(',', ':'), allow_nan=False)


def _restore_rules(context: dict) -> dict:
    context = dict(context)
    if isinstance(context.get('rules'), dict):
        context['rules'] = SymbolRules(**context['rules'])
    return context


class _PlacementJournal:
    """Worker-only, write-before-send journal, partitioned by venue and API key."""

    def __init__(self, path: str, scope: str, diagnostics: DiagnosticsPort | None = None):
        self.path = path
        self.scope = scope
        self._diagnostics = diagnostics

    def run(self, operation: str, request_id: str = "", payload: str = "", fingerprint: str = ""):
        started = time.perf_counter()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS unresolved_placements ("
                    "scope TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, "
                    "payload TEXT NOT NULL, PRIMARY KEY(scope, request_id), UNIQUE(scope, fingerprint))"
                )
                connection.execute('CREATE TABLE IF NOT EXISTS protection_intents ('
                                   'scope TEXT NOT NULL, client_id TEXT NOT NULL, payload TEXT NOT NULL, '
                                   'PRIMARY KEY(scope, client_id))')
                if operation == "load":
                    placements = connection.execute(
                        "SELECT request_id, payload FROM unresolved_placements WHERE scope=?", (self.scope,)
                    ).fetchall()
                    protections = connection.execute('SELECT client_id, payload FROM protection_intents WHERE scope=?', (self.scope,)).fetchall()
                    return {'placements': placements, 'protections': protections}
                if operation == "put":
                    connection.execute(
                        "INSERT INTO unresolved_placements VALUES (?, ?, ?, ?)",
                        (self.scope, request_id, fingerprint, payload),
                    )
                    details = json.loads(payload)
                    context = details.get('context') or {}
                    if context.get('protections') or context.get('protective_leg'):
                        client_id = str(details.get('client_id') or '')
                        record = {'kind': 'leg' if context.get('protective_leg') else 'entry',
                                  'context': context, 'order': details.get('expected') or {},
                                  'client_id': client_id, 'method': details.get('method')}
                        connection.execute('INSERT OR IGNORE INTO protection_intents VALUES (?, ?, ?)',
                                           (self.scope, client_id, _journal_json(record)))
                elif operation == "delete":
                    connection.execute(
                        "DELETE FROM unresolved_placements WHERE scope=? AND request_id=?", (self.scope, request_id)
                    )
                elif operation == 'tranche':
                    records = json.loads(payload)
                    for record in records:
                        connection.execute('INSERT INTO protection_intents VALUES (?, ?, ?) '
                                           'ON CONFLICT(scope, client_id) DO UPDATE SET payload=excluded.payload',
                                           (self.scope, record['client_id'], _journal_json(record)))
                elif operation == 'retire_protection':
                    connection.execute('DELETE FROM protection_intents WHERE scope=? AND client_id=?', (self.scope, request_id))
        finally:
            connection.close()
            if self._diagnostics is not None:
                self._diagnostics.observe_ms("trading.journal_ms", (time.perf_counter() - started) * 1000.0)


class TradingGateway(QtCore.QObject):
    """One-session Binance trading transport with explicit uncertainty handling."""

    MAX_OPEN_ORDER_WRITES = 16
    MAX_ENTRY_WRITES = 8
    MAX_PROTECTIVE_WRITES = 64

    state_changed = Signal(str)
    armed_changed = Signal(bool)
    request_succeeded = Signal(str, object)
    request_failed = Signal(str, str, bool)
    account_event = Signal(object)
    snapshot_ready = Signal(object)
    leverage_changing = Signal(str, int)
    leverage_changed = Signal(str, int, bool, str)
    problem = Signal(str)
    credentials_changed = Signal(str)
    protections_recovered = Signal(object)

    def __init__(self, testnet: bool = False, parent: QtCore.QObject | None = None, *, diagnostics: DiagnosticsPort | None = None):
        super().__init__(parent)
        self._diagnostics = diagnostics
        self.testnet = testnet
        self.rest = BinanceRest(testnet)
        self.api_key = os.getenv("BINANCE_API_KEY", "")
        self.api_secret = os.getenv("BINANCE_API_SECRET", "")
        self.armed = False
        self.stopping = False
        self._lifecycle = 0
        self.trade_socket = QWebSocket()
        self.user_socket = QWebSocket()
        self.trade_connected = False
        self.listen_key = ""
        self.counter = 0
        self.pending: dict[str, dict[str, Any]] = {}
        self.admitted_requests: set[str] = set()
        self.request_details: dict[str, dict[str, Any]] = {}
        self._client_requests: dict[str, str] = {}
        self.failure_contexts: dict[str, dict[str, Any]] = {}
        self.inflight_fingerprints: dict[str, str] = {}
        self.unresolved_fingerprints: dict[str, str] = {}
        self.terminal_requests: set[str] = set()
        self.terminal_request_order: deque[str] = deque()
        self.reconciling: set[str] = set()
        self.tasks: set[ApiTask] = set()


        self.task_pool = QtCore.QThreadPool(self)
        self.task_pool.setMaxThreadCount(20)
        self.task_pool.setExpiryTimeout(30_000)
        self.protection_pool = QtCore.QThreadPool(self)
        self.protection_pool.setMaxThreadCount(4)
        self.account_task: ApiTask | None = None
        self.account_task_scope: tuple[str | None, bool] | None = None
        self.pending_account_refresh: tuple[str | None, bool] | None = None
        self._last_snapshot_mono = 0.0
        self._last_snapshot_scope: tuple[str | None, bool] | None = None
        self._last_account_snapshot: dict | None = None
        self.balance_cache: dict[str, dict[str, Any]] = {}
        self.multi_assets_margin: bool | None = None
        self._refresh_events: list[dict[str, Any]] | None = None
        self._refresh_events_overflow = False
        self._collateral_reservations: dict[str, dict[str, Any]] = {}
        self.account_can_trade: bool | None = None
        self.hedge_mode: bool | None = None
        self.position_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self.leverage_cache: dict[str, int] = {}
        self.account_loaded = False
        self.cross_ready: set[str] = set()
        self.cross_pending: set[str] = set()
        self.queued_leverage: dict[str, int] = {}
        self.stream_generation = 0
        self.user_stream_task: ApiTask | None = None
        self.keepalive_task: ApiTask | None = None
        self.account_event_refresh_symbols: set[str] = set()
        self._socket_bindings = []
        self._bind_session_sockets()
        self.trade_reconnect = QTimer(self)
        self.trade_reconnect.setSingleShot(True)
        self.trade_reconnect.setInterval(2_000)
        self.trade_reconnect.timeout.connect(self._open_trade_socket)
        self.user_reconnect = QTimer(self)
        self.user_reconnect.setSingleShot(True)
        self.user_reconnect.setInterval(2_500)
        self.user_reconnect.timeout.connect(self._resume_user_stream)
        self.keepalive_timer = QTimer(self)
        self.keepalive_timer.setInterval(45 * 60 * 1_000)
        self.keepalive_timer.timeout.connect(self._keepalive_user_stream)
        self.pending_timeout_timer = QTimer(self)
        self.pending_timeout_timer.setInterval(1_000)
        self.pending_timeout_timer.timeout.connect(self._expire_pending_requests)
        self.account_event_refresh_timer = QTimer(self)
        self.account_event_refresh_timer.setSingleShot(True)
        self.account_event_refresh_timer.setInterval(1_500)
        self.account_event_refresh_timer.timeout.connect(self._refresh_after_account_event)
        self.protection_poll_timer = QTimer(self)
        self.protection_poll_timer.setInterval(5_000)
        self.protection_poll_timer.timeout.connect(self._poll_disconnected_protections)
        self._journal_pool = QtCore.QThreadPool(self)
        self._journal_pool.setMaxThreadCount(1)
        self._journal: _PlacementJournal | None = None
        self._journal_loading = True
        self._journal_error = ""
        self._journal_requests: set[str] = set()
        self._protection_records: dict[str, dict[str, Any]] = {}
        self._protection_cleanup_clients: set[str] = set()
        self._protection_cleanup_attempts: dict[str, int] = {}
        self._protection_cancel_tokens: dict[str, threading.Event] = {}
        self._protection_recovery_pending = False
        self._protection_recovery_task = None
        self._protection_reserved_slots = 0
        QTimer.singleShot(0, self, self._restore_placement_journal)

    def _journal_failed(self, message: str) -> None:
        self._journal_loading = False
        self._journal_error = str(message)
        if self._diagnostics is not None:
            self._diagnostics.error("TRADING JOURNAL", message)
        self.problem.emit(f"Order persistence unavailable; new placements are blocked: {message}")

    def _restore_placement_journal(self) -> None:
        if not self.has_credentials() or self.stopping:
            return
        scope = ("testnet:" if self.testnet else "mainnet:") + hashlib.sha256(self.api_key.encode()).hexdigest()
        if self._journal is not None and self._journal.scope == scope:
            return
        directory = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.StandardLocation.AppDataLocation)
        if not directory:
            self._journal_failed("The application data directory is unavailable.")
            return
        journal = _PlacementJournal(os.path.join(directory, "unresolved_placements.sqlite3"), scope, self._diagnostics)
        self._journal = journal
        self._journal_loading = True
        self._journal_error = ""

        def restored(saved) -> None:
            try:
                records = {client_id: json.loads(payload) for client_id, payload in saved['protections']}
                for record in records.values():
                    record['context'] = _restore_rules(record.get('context') or {})
                    record['_status_received_mono'] = 0.0
                restored_details = []
                for request_id, payload in saved['placements']:
                    details = json.loads(payload)
                    if details.get("method") not in {"order.place", "algoOrder.place", "batchOrders.place"} or not details.get("fingerprint"):
                        raise ValueError("Invalid saved order intent.")
                    context = details.get("context") or {}
                    if isinstance(context.get("rules"), dict):
                        context["rules"] = SymbolRules(**context["rules"])
                    details["context"] = context
                    details["transport_state"] = "uncertain"
                    restored_details.append((request_id, details))
            except (TypeError, ValueError, AttributeError) as exc:
                self._journal_failed(f"Saved placements could not be read: {exc}")
                return
            self._protection_records = records
            self._protection_recovery_pending = bool(records)
            unknown_count = 0
            for request_id, details in restored_details:
                self._journal_requests.add(request_id)
                record = records.get(str(details.get('client_id') or ''))
                if record is not None and record.get('not_accepted') and record.get('method') == details.get('method'):
                    # The final protection state may commit before deletion of
                    # its placement row. It already proves this write failed or
                    # was canceled before transmission; do not query it forever.
                    self._mark_terminal(request_id)
                    continue
                self._set_request_details(request_id, details)
                self.unresolved_fingerprints[details["fingerprint"]] = request_id
                unknown_count += 1
            self._journal_loading = False
            if records:
                self._recover_protections()
            if unknown_count:
                self.state_changed.emit(f"RESTORED {unknown_count} UNRESOLVED ORDER INTENTS")
                self.reconcile_unknown_orders()

        task = self._launch_task(lambda: journal.run("load"), restored, self._journal_failed, self._journal_pool)
        self.tasks.add(task)

    def submit_protection_tranche(self, plan: dict, legs: list[tuple[dict, dict]]) -> None:
        """Persist the whole tranche before sending any of its individual legs.

        A crash between leg transmissions leaves every intended client ID
        available for reconciliation. An absent/unknown ID is never resent.
        """
        journal = self._journal
        if journal is None or self._journal_error:
            for _order, context in legs:
                self._remember_failure(self._next_id('protection'), 'Protection journal unavailable.', False, {'context': context})
            return
        used = self._open_order_write_count(protective=True)
        if used + self._protection_reserved_slots + len(legs) > self.MAX_PROTECTIVE_WRITES:
            if legs:
                self._remember_failure(self._next_id('protection'), 'Protection capacity exhausted; the new fill tranche requires a risk-reducing close.', False, {'context': legs[0][1]})
            return
        client_id = str(plan.get('client_id') or '')
        entry = dict(plan.get('entry') or {})
        previous = self._protection_records.get(client_id, {})
        records = [{'kind': 'entry', 'client_id': client_id, 'context': dict(plan), 'order': entry,
                    'last_status': previous.get('last_status', ''),
                    '_status_received_mono': previous.get('_status_received_mono', 0.0),
                    'execution': dict(previous.get('execution') or {}),
                    'method': 'algoOrder.place' if str(entry.get('type')) in CONDITIONAL_ORDER_TYPES else 'order.place'}]
        for order, context in legs:
            records.append({'kind': 'leg', 'client_id': context['client_id'], 'context': dict(context),
                            'order': dict(order), 'method': 'algoOrder.place'})
            self._protection_cancel_tokens.setdefault(context['client_id'], threading.Event())
        serialized = _journal_json(records)
        self._protection_reserved_slots += len(legs)
        self._protection_records.update({record['client_id']: record for record in records})

        def committed(_result):
            self._protection_reserved_slots -= len(legs)
            for order, context in legs:
                self.submit_order(order, context)

        def failed(message):
            self._protection_reserved_slots -= len(legs)
            self._journal_failed(message)
            self._protection_recovery_pending = True
            self.problem.emit('Protection tranche was not sent. Inspect exposure and the saved plan before resuming entries.')

        task = self._launch_task(lambda: journal.run('tranche', payload=serialized), committed, failed, self._journal_pool)
        self.tasks.add(task)

    def _recover_protections(self) -> None:
        if self._protection_recovery_task is not None or not self._protection_records or self.stopping:
            return
        if not self.account_loaded:
            self.refresh_account(None, all_open_orders=True, follow_up=True)
            return
        records = [dict(record) for record in self._protection_records.values()]
        query_started = time.monotonic()
        self._protection_recovery_pending = True
        api_key, api_secret = self.api_key, self.api_secret

        def query():
            recovered, errors = [], []
            for record in records:
                order = record['order']
                symbol = str(order.get('symbol') or record['context'].get('symbol') or '')
                try:
                    if record.get('not_accepted'):
                        recovered.append({**record, 'result': {'symbol': symbol, 'status': record.get('last_status') or 'REJECTED', 'executedQty': '0'}})
                        continue
                    result = normalize_order(self.rest.query_order_by_client_id(api_key, api_secret, symbol,
                                                               record['client_id'], algo=record['method'] == 'algoOrder.place'))
                    result = self._with_child_execution(api_key, api_secret, symbol, result)
                    recovered.append({**record, 'result': result})
                except (RuntimeError, OSError, TimeoutError) as exc:
                    errors.append(f"{symbol} · {record['client_id']} · {exc}")
            return {'records': recovered, 'errors': errors}

        def done(result):
            self._protection_recovery_task = None
            restored = []
            for recovered in result['records']:
                current = self._protection_records.get(recovered['client_id'])
                if current is None:
                    continue
                status = str(recovered['result'].get('status') or recovered['result'].get('algoStatus') or '').upper()
                if current.get('_status_received_mono', 0.0) <= query_started:
                    self._observe_protection_order(recovered['client_id'], recovered['result'], received_at=query_started)
                recovered['result'] = {**recovered['result'], **current.get('execution', {}), 'status': current.get('last_status') or status}
                restored.append({**recovered, **current})
            result = {**result, 'records': restored}
            self._protection_recovery_pending = bool(result['errors'])
            # A terminal entry may have filled and closed while this app was
            # offline. Read positions after observing that terminal status
            # before extending its protection or retiring its saved plan.
            flat_unconfirmed = any(
                (record.get('last_status') in {'FILLED', 'FINISHED', 'CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'REJECTED'}
                 or (record['kind'] == 'entry' and safe_float(record.get('execution', {}).get('executedQty')) > 0))
                and not self.has_open_position(str(record['order'].get('symbol') or ''), str(record['order'].get('positionSide') or 'BOTH'))
                and record.get('_status_received_mono', 0.0) > self._last_snapshot_mono
                for record in restored
            )
            if flat_unconfirmed:
                self._protection_recovery_pending = True
                self.refresh_account(None, all_open_orders=True, follow_up=True)
                return
            for record in restored:
                symbol = str(record['order'].get('symbol') or '')
                if (record['kind'] == 'leg' and record.get('last_status') in {'REJECTED', 'EXPIRED', 'EXPIRED_IN_MATCH'}
                        and self.has_open_position(symbol, str(record['order'].get('positionSide') or 'BOTH'))):
                    result['errors'].append(f"{symbol} · {record['client_id']} · recovered protection ended with {record['last_status']}; inspect exposure")
            self._protection_recovery_pending = bool(result['errors'])
            if not result['errors'] and self._last_account_snapshot is not None:
                self._retire_closed_protections(self._last_account_snapshot)
            self.protections_recovered.emit(result)
            if result['errors']:
                self.problem.emit('Protection recovery requires review; new entries are blocked and no missing leg was resent. Use Reconcile after inspecting orders/positions. ' + '; '.join(result['errors']))

        def failed(message):
            self._protection_recovery_task = None
            self.problem.emit('Protection recovery failed; new entries remain blocked: ' + message)

        task = self._launch_task(query, done, failed, self.task_pool)
        self._protection_recovery_task = task
        self.tasks.add(task)

    def _persist_protection_record(self, record: dict) -> None:
        if self._journal is None:
            return
        journal = self._journal
        serialized = _journal_json([record])
        task = self._launch_task(lambda: journal.run('tranche', payload=serialized), lambda _result: None, self._journal_failed, self._journal_pool)
        self.tasks.add(task)

    def protection_client_id(self, source: dict) -> str:
        """Resolve engine order updates/rejections back to their saved Algo plan."""
        order = normalize_order(source)
        direct = str(order.get('clientAlgoId') or order.get('clientOrderId') or '')
        if direct in self._protection_records:
            return direct
        symbol = str(order.get('symbol') or '')
        identifier = str(order.get('orderId') or '')
        if symbol and identifier:
            for client_id, record in self._protection_records.items():
                if str(record.get('order', {}).get('symbol') or '') != symbol:
                    continue
                execution = record.get('execution') or {}
                if identifier in {str(execution.get('actualOrderId') or ''), str(execution.get('orderId') or '')}:
                    return client_id
        return direct

    def _observe_protection_order(self, client_id: str, source: dict, *, received_at: float | None = None) -> None:
        record = self._protection_records.get(client_id)
        if record is None:
            return
        order = normalize_order(source)
        previous_execution = dict(record.get('execution') or {})
        if not valid_order_fills(order):
            self._protection_recovery_pending = True
            self.problem.emit('Invalid protection fill data; reconciling before changing saved execution state.')
            self._queue_account_refresh(str(record.get('order', {}).get('symbol') or ''))
            return
        stamp = event_timestamp({'T': order.get('updateTime')}, order)
        if (older_than_row(stamp, previous_execution)
                or ('executedQty' in order and safe_float(order['executedQty']) < safe_float(previous_execution.get('executedQty')))):
            return
        execution = dict(previous_execution)
        for key in ('orderId', 'algoId', 'actualOrderId', 'actualType', 'avgPrice'):
            if order.get(key) not in (None, '', '0', 0):
                execution[key] = order[key]
        if safe_float(order.get('executedQty')) >= safe_float(execution.get('executedQty')):
            execution['executedQty'] = order.get('executedQty', execution.get('executedQty', '0'))
        if stamp:
            execution['updateTime'] = stamp
        record['execution'] = execution
        status = str(order.get('status') or record.get('last_status') or 'NEW').upper()
        previous_status = record.get('last_status')
        terminal = {'CANCELED', 'FILLED', 'FINISHED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'REJECTED'}
        verified_child = (order.get('_child_reconciled') or
                          (order.get('orderId') and str(order['orderId']) == str(previous_execution.get('actualOrderId'))))
        if previous_status in terminal and status not in terminal and not (previous_status == 'FINISHED' and verified_child):
            status = previous_status
        changed = execution != previous_execution or status != record.get('last_status')
        record['last_status'] = status
        if changed:
            record['_status_received_mono'] = received_at if received_at is not None else time.monotonic()
            self._persist_protection_record(record)
        if status in {'CANCELED', 'FILLED', 'FINISHED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'REJECTED'}:
            self._protection_cleanup_clients.discard(client_id)
        if record.get('kind') == 'leg':
            self._cancel_completed_protection_tranche(record)

    def _with_child_execution(self, api_key: str, api_secret: str, symbol: str, source: dict) -> dict:
        result = normalize_order(source)
        if not valid_order_fills(result):
            raise RuntimeError('Unexpected response: Binance returned invalid order fill data.')
        child_id = result.get('actualOrderId')
        if safe_float(child_id) > 0:
            try:
                child = normalize_order(self.rest.query_order(api_key, api_secret, symbol, child_id))
            except RuntimeError as exc:
                # Trigger rejection can produce an engine ID without a history
                # row. A terminal Algo parent remains authoritative in that case.
                if binance_error_code(exc) != -2013 or result.get('status') not in {'FINISHED', 'REJECTED', 'CANCELED', 'EXPIRED'}:
                    raise
                return {**result, '_child_reconciled': True, '_child_missing': True}
            result.update(status=child.get('status') or result.get('status'),
                          executedQty=child.get('executedQty', result.get('executedQty', '0')),
                          avgPrice=child.get('avgPrice', result.get('avgPrice', '0')),
                          _child_reconciled=True)
            if not valid_order_fills(result):
                raise RuntimeError('Unexpected response: Binance returned invalid child order fill data.')
        elif result.get('algoId') and result.get('status') == 'FINISHED':
            raise RuntimeError('Unexpected response: finished Algo order omitted its matching-engine order identifier.')
        return result

    def _cancel_protection_record(self, record: dict) -> None:
        client_id = str(record['client_id'])
        self._protection_cleanup_clients.add(client_id)
        token = self._protection_cancel_tokens.get(client_id)
        if token is not None:
            token.set()
        execution = record.get('execution') or {}
        if (not (execution.get('algoId') or execution.get('orderId'))
                and (self._client_requests.get(client_id) in self.admitted_requests or token is not None)):
            # A durable placement may still be waiting for its worker or clock
            # sync. Stop it locally, or cancel after its acceptance is observed.
            return
        if any(details.get('context', {}).get('cleanup_client') == client_id for details in self.request_details.values()):
            return
        attempts = self._protection_cleanup_attempts.get(client_id, 0)
        if attempts >= 3:
            return
        self._protection_cleanup_attempts[client_id] = attempts + 1
        symbol = str(record.get('order', {}).get('symbol') or '')
        if safe_float(execution.get('actualOrderId')) > 0:
            request, algo = {'symbol': symbol, 'orderId': execution['actualOrderId']}, False
        else:
            request, algo = {'symbol': symbol, 'clientAlgoId': client_id}, True
        self.submit_cancel(request, algo, context={'cleanup_client': client_id})

    def _cancel_completed_protection_tranche(self, filled_leg: dict) -> None:
        context = filled_leg.get('context') or {}
        entry_id, fill_total = context.get('entry_client_id'), context.get('fill_total')
        if not entry_id or not fill_total:
            return
        legs = [record for record in self._protection_records.values() if record.get('kind') == 'leg'
                and record.get('context', {}).get('entry_client_id') == entry_id
                and record.get('context', {}).get('fill_total') == fill_total]
        exited = sum((Decimal(str(record.get('execution', {}).get('executedQty') or '0')) for record in legs), Decimal(0))
        amount = Decimal(str(context.get('tranche_quantity') or '0'))
        if not amount.is_finite() or amount <= 0 or exited < amount:
            return
        for record in legs:
            if record.get('last_status') not in {'CANCELED', 'FILLED', 'FINISHED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'REJECTED'}:
                self._cancel_protection_record(record)

    def remaining_protection_quantity(self, context: dict) -> Decimal:
        """Bound fail-safe closes to the part of this fill tranche not exited."""
        amount = Decimal(str(context.get('tranche_quantity') or context.get('quantity') or '0'))
        entry_id, fill_total = context.get('entry_client_id'), context.get('fill_total')
        if not entry_id or not fill_total:
            return amount
        exited = sum((Decimal(str(record.get('execution', {}).get('executedQty') or '0'))
                      for record in self._protection_records.values()
                      if record.get('kind') == 'leg' and record.get('context', {}).get('entry_client_id') == entry_id
                      and record.get('context', {}).get('fill_total') == fill_total), Decimal(0))
        if not amount.is_finite() or not exited.is_finite() or amount < 0 or exited < 0:
            raise ValueError('Protection tranche execution quantities could not be verified.')
        return max(Decimal(0), amount - exited)

    def _retire_closed_protections(self, snapshot: dict) -> None:
        if self._journal is None or self._protection_recovery_pending or self._protection_recovery_task is not None:
            return
        scope = str(snapshot.get('ordersScope') or '')
        live_ids = {str(row.get('clientOrderId') or row.get('clientAlgoId') or '') for row in [*snapshot.get('orders', []), *snapshot.get('algoOrders', [])]}
        live_engine_ids = {(str(row.get('symbol') or ''), str(row.get('orderId')))
                           for row in snapshot.get('orders', []) if row.get('orderId')}
        terminal = {'FILLED', 'FINISHED', 'CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'REJECTED'}
        for client_id, entry in tuple(self._protection_records.items()):
            if entry['kind'] != 'entry' or str(entry.get('last_status') or '').upper() not in terminal:
                continue
            symbol = str(entry['order'].get('symbol') or '')
            position_side = str(entry['order'].get('positionSide') or 'BOTH')
            if scope not in {'ALL', symbol} or entry.get('_status_received_mono', 0) > snapshot.get('_read_started_mono', 0):
                continue
            related = [key for key, record in self._protection_records.items() if key == client_id or record['context'].get('entry_client_id') == client_id]
            unfilled_entry = (len(related) == 1 and entry.get('last_status') in {'CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'REJECTED'}
                              and safe_float(entry.get('execution', {}).get('executedQty'), -1) == 0
                              and safe_float(entry.get('context', {}).get('filled_quantity')) <= 0
                              and safe_float(entry.get('context', {}).get('protected_quantity')) <= 0)
            has_position = self.has_open_position(symbol, position_side) and not unfilled_entry
            engine_ids = {str(self._protection_records[key].get('execution', {}).get('actualOrderId') or '') for key in related}
            live_engine = any((symbol, engine_id) in live_engine_ids for engine_id in engine_ids)
            entry_engine_id = str(entry.get('execution', {}).get('actualOrderId') or '')
            live_entry_engine = (symbol, entry_engine_id) in live_engine_ids
            pending_related = any(
                self._client_requests.get(key) in self.request_details
                or (key in self._protection_cancel_tokens
                    and self._protection_records[key].get('last_status') not in terminal
                    and not self._protection_records[key].get('execution', {}).get('algoId')
                    and not self._protection_records[key].get('execution', {}).get('orderId'))
                for key in related
            )
            recent_related = any(self._protection_records[key].get('_status_received_mono', 0) > snapshot.get('_read_started_mono', 0)
                                 for key in related)
            if recent_related:
                self._queue_account_refresh(symbol)
            if has_position or any(key in live_ids for key in related) or live_engine or pending_related or recent_related:
                if not has_position and not live_entry_engine and client_id not in live_ids:
                    for key in related:
                        record = self._protection_records[key]
                        if (record.get('kind') == 'leg'
                                and (key in live_ids or (key in self._protection_cancel_tokens and record.get('last_status') not in terminal)
                                     or (record.get('last_status') not in terminal
                                         and (record.get('execution', {}).get('algoId') or record.get('execution', {}).get('orderId')))
                                     or (symbol, str(record.get('execution', {}).get('actualOrderId') or '')) in live_engine_ids)):
                            self._cancel_protection_record(record)
                continue
            journal = self._journal
            for key in related:
                self._protection_records.pop(key, None)
                self._protection_cleanup_clients.discard(key)
                self._protection_cleanup_attempts.pop(key, None)
                token = self._protection_cancel_tokens.pop(key, None)
                if token is not None:
                    token.set()
                task = self._launch_task(lambda key=key: journal.run('retire_protection', key), lambda _result: None, self._journal_failed, self._journal_pool)
                self.tasks.add(task)
        if not self._protection_records:
            self._protection_recovery_pending = False

    def _queue_placement(self, request_id: str, details: dict[str, Any], transmit) -> None:
        """Admit once; transmission waits for a durable worker commit."""
        journal = self._journal
        if self._journal_loading or self._journal_error or journal is None:
            self._remember_failure(request_id, self._journal_error or "Saved order outcomes are still loading; try again after reconciliation starts.", False, details)
            return

        saved = {key: details[key] for key in ("method", "expected", "expected_orders", "client_id", "client_ids", "fingerprint") if key in details}
        context = {key: value for key, value in dict(details.get("context") or {}).items() if key in {
            "entry", "protections", "rules", "position_intent", "protective_leg", "kind", "symbol",
            "entry_side", "position_side", "quantity", "entry_client_id", "client_id",
            "emergency_close", "reason", "reserve_key", "reserved", "existing_order_replacement",
            "collateral_asset", "collateral_required", "fill_total", "tranche_quantity", "target_index",
        }}
        if isinstance(context.get("rules"), SymbolRules):
            context["rules"] = asdict(context["rules"])
        saved["context"] = context

        def unsigned(value):
            if isinstance(value, dict):
                return {
                    key: unsigned(item) for key, item in value.items()
                    if str(key).replace("_", "").casefold() not in {"apikey", "apisecret", "secret", "signature"}
                }
            if isinstance(value, (list, tuple)):
                return [unsigned(item) for item in value]
            return value

        try:
            serialized = _journal_json(unsigned(saved))
        except (TypeError, ValueError) as exc:
            self._remember_failure(request_id, f"Order intent could not be saved: {exc}", False, details)
            return
        self._set_request_details(request_id, details)
        self.inflight_fingerprints[str(details["fingerprint"])] = request_id
        self.admitted_requests.add(request_id)
        self._journal_requests.add(request_id)

        def committed(_result) -> None:
            if context.get('protections') or context.get('protective_leg'):
                client_id = str(details.get('client_id') or '')
                self._protection_records.setdefault(client_id, {'kind': 'leg' if context.get('protective_leg') else 'entry',
                    'client_id': client_id, 'method': details['method'], 'order': details.get('expected') or {},
                    'context': _restore_rules(context)})
            details["_journaled"] = True
            transmit()

        def failed(message: str) -> None:
            self.admitted_requests.discard(request_id)
            self._journal_failed(message)
            self._remember_failure(request_id, f"Order was not transmitted: {message}", False, details)

        task = self._launch_task(
            lambda: journal.run("put", request_id, serialized, str(details["fingerprint"])),
            committed, failed, self._journal_pool,
        )
        self.tasks.add(task)

    def _bind_session_sockets(self) -> None:
        for signal, slot in self._socket_bindings:
            signal.disconnect(slot)
        self._socket_bindings.clear()
        lifecycle = self._lifecycle
        for socket, callbacks in (
            (self.trade_socket, (self._trade_connected, self._trade_disconnected, self._trade_message, self._trade_error)),
            (self.user_socket, (self._user_connected, self._user_disconnected, self._user_message, self._user_error)),
        ):
            for name, callback in zip(('connected', 'disconnected', 'textMessageReceived', 'errorOccurred'), callbacks):
                def deliver(*args, callback=callback, lifecycle=lifecycle):
                    if lifecycle == self._lifecycle and not self.stopping:
                        callback(*args)
                signal = getattr(socket, name)
                signal.connect(deliver)
                self._socket_bindings.append((signal, deliver))

    def _launch_task(self, function, finished, failed, pool=None) -> ApiTask:
        lifecycle = self._lifecycle
        task: ApiTask

        def execute():
            if lifecycle != self._lifecycle or self.stopping:
                raise RuntimeError('Trading session stopped before transmission.')
            return function()

        def done(result):
            self.tasks.discard(task)
            if lifecycle == self._lifecycle and not self.stopping:
                finished(result)

        def error(message):
            self.tasks.discard(task)
            if lifecycle == self._lifecycle and not self.stopping:
                failed(message)

        task = launch_task(execute, done, error, pool or self.task_pool, source='execution/account')
        return task

    def _set_request_details(self, request_id: str, details: dict[str, Any]) -> None:
        self._pop_request_details(request_id)
        self.request_details[request_id] = details
        client_id = str(details.get('client_id') or '')
        if client_id and request_id not in self.terminal_requests:
            self._client_requests[client_id] = request_id

    def _pop_request_details(self, request_id: str, default=None):
        details = self.request_details.pop(request_id, default)
        client_id = str((details or {}).get('client_id') or '')
        if client_id and self._client_requests.get(client_id) == request_id:
            self._client_requests.pop(client_id, None)
        return details

    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def set_credentials(self, api_key: str, api_secret: str) -> None:
        updated_key = api_key.strip()
        updated_secret = api_secret.strip()
        changed = (updated_key, updated_secret) != (self.api_key, self.api_secret)
        if not changed:
            return
        if changed and (
            self.pending
            or self.admitted_requests
            or self.reconciling
            or self.unresolved_fingerprints
            or self.cross_pending
            or self.account_task is not None
            or self._protection_records
            or any(details.get('transport_state') == 'uncertain' for details in self.request_details.values())
        ):
            self.problem.emit(
                "Credentials cannot be replaced while a request is in flight, unresolved, or protection monitoring is active. "
                "Reconcile requests and close/retire the saved protection plans first."
            )
            return
        self.stream_generation += 1
        self._lifecycle += 1
        self._client_requests.clear()
        self.api_key = updated_key
        self.api_secret = updated_secret
        self.disarm()
        self.trade_reconnect.stop()
        self.user_reconnect.stop()
        self.keepalive_timer.stop()
        self.account_event_refresh_timer.stop()
        self.protection_poll_timer.stop()
        self.trade_socket.close()
        self.user_socket.close()
        self.trade_connected = False
        self._bind_session_sockets()
        self.listen_key = ""
        self.user_stream_task = None
        self.keepalive_task = None
        self.balance_cache.clear()
        self._collateral_reservations.clear()
        self.multi_assets_margin = None
        self._refresh_events = None
        self.account_can_trade = None
        self.position_cache.clear()
        self.leverage_cache.clear()
        self.hedge_mode = None
        self.account_loaded = False
        self.pending_account_refresh = None
        self._last_snapshot_mono = 0.0
        self._last_snapshot_scope = None
        self._last_account_snapshot = None
        self._protection_cleanup_clients.clear()
        self._protection_cleanup_attempts.clear()
        self._protection_cancel_tokens.clear()
        self.account_event_refresh_symbols.clear()
        self.cross_ready.clear()
        self.cross_pending.clear()
        self._journal = None
        self._journal_loading = True
        self._journal_error = ""
        self._restore_placement_journal()
        if self.has_credentials():
            self.state_changed.emit("CREDENTIALS READY · MANUAL TRADING")
        else:
            self.state_changed.emit("API CREDENTIALS REQUIRED")
        self.credentials_changed.emit(self.api_key)

    def arm(self) -> bool:
        if not self.has_credentials():
            self.problem.emit("Add Binance API credentials before arming live trading.")
            return False
        if self.account_can_trade is not True:
            self.problem.emit(
                "Refresh the account and verify Binance trading permission before arming."
            )
            return False
        self.armed = True
        self.stopping = False
        self._bind_session_sockets()
        self.armed_changed.emit(True)
        self.state_changed.emit("ARMED · CONNECTING ORDER LINK")
        self._open_trade_socket()
        self._start_user_stream()
        if self.listen_key:
            self._keepalive_user_stream()
        return True

    def disarm(self) -> None:
        if self.armed:
            self.armed = False
            self.armed_changed.emit(False)
        self.state_changed.emit("QUICK KEYS DISARMED · MANUAL TRADING READY")

    def connect_session(self) -> None:
        """Keep account/order transport independent of quick-key arming."""
        if self.stopping or not self.has_credentials():
            return
        self._open_trade_socket()
        self._start_user_stream()

    def _next_id(self, prefix: str = "req") -> str:
        self.counter = (self.counter + 1) % 1_000_000
        return f"{prefix}-{int(time.time() * 1000)}-{self.counter}"

    def client_order_id(self, prefix: str = "nw") -> str:
        return self._next_id(prefix).replace("-", "_")[:36]

    def request_was_admitted(self, request_id: str) -> bool:
        """True when accepted by the durable placement queue or a transport."""
        return request_id in self.admitted_requests

    def failure_context(self, request_id: str) -> dict[str, Any]:
        return dict(self.failure_contexts.pop(request_id, {}))

    def _open_order_write_count(self, *, protective: bool | None = None) -> int:
        return sum(
            len(details.get("expected_orders", []))
            if details.get("method") == "batchOrders.place"
            else 1
            for request_id, details in self.request_details.items()
            if request_id not in self.terminal_requests
            and details.get("method")
            in {"order.place", "algoOrder.place", "batchOrders.place"}
            and (protective is None or bool((details.get('context') or {}).get('protective_leg')) == protective)
        )

    def placement_status(self) -> tuple[int, int, int]:
        """Used slots, limit and unresolved requests (including restored intents)."""
        unknown = len(set(self.unresolved_fingerprints.values()) - self.terminal_requests)
        return (self._open_order_write_count() + self._protection_reserved_slots,
                self.MAX_OPEN_ORDER_WRITES + self.MAX_PROTECTIVE_WRITES, unknown)

    def _mark_terminal(self, request_id: str) -> None:
        """Remember final outcomes so REST/stream races cannot complete twice."""
        if request_id in self.terminal_requests:
            return
        if request_id in self._journal_requests and self._journal is not None:
            self._journal_requests.discard(request_id)
            journal = self._journal
            task = self._launch_task(
                lambda: journal.run("delete", request_id), lambda _result: None,
                self._journal_failed, self._journal_pool,
            )
            self.tasks.add(task)
        while len(self.terminal_request_order) >= 4096:
            self.terminal_requests.discard(self.terminal_request_order.popleft())
        self.terminal_requests.add(request_id)
        self.terminal_request_order.append(request_id)

    @staticmethod
    def _order_fingerprint(order: dict[str, Any]) -> str:
        ignored = {
            "newClientOrderId", "clientAlgoId", "timestamp", "recvWindow",
            "signature", "apiKey", "newOrderRespType",
        }
        normalized = {
            key: str(value)
            for key, value in order.items()
            if key not in ignored and value is not None and value != ""
        }
        return hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _validate_order_payload(
        order: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        payload = dict(order)
        symbol = str(payload.get("symbol", "")).upper().strip()
        side = str(payload.get("side", "")).upper().strip()
        order_type = str(payload.get("type", "")).upper().strip()
        position_side = str(payload.get("positionSide", "BOTH")).upper().strip()
        for key in ('reduceOnly', 'closePosition', 'priceProtect'):
            if key in payload:
                flag = exchange_bool(payload[key])
                if flag is None:
                    raise ValueError(f'{key} must be true or false.')
                payload[key] = flag
        reduce_only = payload.get('reduceOnly', False)
        close_position = payload.get('closePosition', False)
        payload.update(
            {
                "symbol": symbol,
                "side": side,
                "type": order_type,
                "positionSide": position_side,
            }
        )
        if "reduceOnly" in payload:
            payload["reduceOnly"] = reduce_only
        if "closePosition" in payload:
            payload["closePosition"] = close_position
        if not re.fullmatch(r"[A-Z0-9]{5,24}", symbol):
            raise ValueError("Order symbol is missing or invalid.")
        if side not in {"BUY", "SELL"}:
            raise ValueError("Order side must be BUY or SELL.")
        if order_type not in STANDARD_ORDER_TYPES:
            raise ValueError(f"Unsupported Binance USD-M order type: {order_type or 'blank'}.")
        if position_side not in {"BOTH", "LONG", "SHORT"}:
            raise ValueError("Position side must be BOTH, LONG or SHORT.")
        if position_side != "BOTH" and reduce_only:
            raise ValueError("Binance hedge-mode orders cannot include reduceOnly.")
        if position_side != 'BOTH':
            payload.pop('reduceOnly', None)
        if close_position:
            if order_type not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}:
                raise ValueError(
                    "closePosition is valid only for Stop Market and Take Profit Market orders."
                )
            if payload.get("quantity") not in (None, ""):
                raise ValueError("A close-all conditional order cannot include quantity.")
            if reduce_only:
                raise ValueError("closePosition and reduceOnly cannot be sent together.")
            payload.pop('reduceOnly', None)
            if position_side == "LONG" and side == "BUY":
                raise ValueError("Closing a hedge-mode LONG requires SELL/LONG.")
            if position_side == "SHORT" and side == "SELL":
                raise ValueError("Closing a hedge-mode SHORT requires BUY/SHORT.")
        intent = str(context.get("position_intent") or "").upper()
        if intent == "REDUCE":
            if position_side == "BOTH" and not (reduce_only or close_position):
                raise ValueError("A one-way reduce order must include reduceOnly.")
            if position_side == "LONG" and side != "SELL":
                raise ValueError("Reducing a hedge-mode LONG requires SELL/LONG.")
            if position_side == "SHORT" and side != "BUY":
                raise ValueError("Reducing a hedge-mode SHORT requires BUY/SHORT.")
        elif intent == "OPEN":
            if reduce_only or close_position:
                raise ValueError("An open-position order cannot include reduceOnly.")
            if position_side == "LONG" and side != "BUY":
                raise ValueError("Opening a hedge-mode LONG requires BUY/LONG.")
            if position_side == "SHORT" and side != "SELL":
                raise ValueError("Opening a hedge-mode SHORT requires SELL/SHORT.")
        rules = context.get("rules")
        if isinstance(rules, SymbolRules) and not close_position:
            market_quantity = order_type in {
                "MARKET", "STOP_MARKET", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"
            }
            step = (
                rules.market_step
                if market_quantity
                else rules.lot_step
            )
            payload["quantity"] = validate_step(
                str(payload.get("quantity", "")), step, "Quantity", offset=str(rules.min_market_qty if market_quantity else rules.min_qty)
            )
            quantity = safe_float(payload["quantity"])
            minimum_qty = rules.min_market_qty if market_quantity else rules.min_qty
            maximum_qty = rules.max_market_qty if market_quantity else rules.max_qty
            if quantity <= 0 or not (minimum_qty <= quantity <= maximum_qty):
                raise ValueError(
                    f"Quantity must be between {minimum_qty:g} and {maximum_qty:g}."
                )
            if market_quantity:
                validate_step(payload['quantity'], rules.lot_step, 'Quantity', offset=str(rules.min_qty))
                if not (rules.min_qty <= quantity <= rules.max_qty):
                    raise ValueError('Market quantity is outside Binance LOT_SIZE limits.')
        elif not close_position and safe_float(payload.get("quantity")) <= 0:
            raise ValueError("Order quantity must be positive.")
        for key, label in (
            ('price', 'Order price'), ('triggerPrice', 'Trigger price'),
            ('activatePrice', 'Activation price'),
        ):
            if payload.get(key) in (None, ''):
                continue
            if isinstance(rules, SymbolRules):
                payload[key] = validate_step(str(payload[key]), rules.tick_size, label, offset=str(rules.min_price))
                price = safe_float(payload[key])
                if price <= 0 or price < rules.min_price or (rules.max_price > 0 and price > rules.max_price):
                    raise ValueError(f'{label} is outside Binance symbol price limits.')
            elif safe_float(payload[key]) <= 0:
                raise ValueError(f'{label} must be positive.')
        working_type = str(payload.get("workingType", "MARK_PRICE")).upper()
        if working_type not in {"MARK_PRICE", "CONTRACT_PRICE"}:
            raise ValueError("Working type must be MARK_PRICE or CONTRACT_PRICE.")
        if "workingType" in payload:
            payload["workingType"] = working_type
        price_match = str(payload.get("priceMatch", "")).upper().strip()
        if price_match:
            if price_match not in {
                "OPPONENT", "OPPONENT_5", "OPPONENT_10", "OPPONENT_20",
                "QUEUE", "QUEUE_5", "QUEUE_10", "QUEUE_20",
            }:
                raise ValueError("Unsupported Binance price-match mode.")
            if order_type not in {"LIMIT", "STOP", "TAKE_PROFIT"}:
                raise ValueError("priceMatch is not supported for this order type.")
            if payload.get("price") not in (None, ""):
                raise ValueError("price and priceMatch cannot be sent together.")
            payload["priceMatch"] = price_match
        if order_type in {"LIMIT", "STOP", "TAKE_PROFIT"}:
            if not price_match and safe_float(payload.get("price")) <= 0:
                raise ValueError(f"{order_type} requires a positive order price.")
            time_in_force = str(payload.get("timeInForce") or ('GTC' if order_type in CONDITIONAL_ORDER_TYPES else '')).upper()
            if time_in_force not in {"GTC", "IOC", "FOK", "GTX", "GTD", "RPI"}:
                raise ValueError("A valid time-in-force is required for this order type.")
            payload["timeInForce"] = time_in_force
            if time_in_force == "GTD":
                good_till = int(safe_float(payload.get("goodTillDate"))) // 1000 * 1000
                now_ms = int(context.get('_server_time_ms') or time.time() * 1_000)
                if not (now_ms + 600_000 < good_till < 253_402_300_799_000):
                    raise ValueError(
                        "GTD goodTillDate must be more than 600 seconds ahead and within Binance's supported range."
                    )
                payload['goodTillDate'] = good_till
        stp_mode = str(payload.get("selfTradePreventionMode", "")).upper().strip()
        if stp_mode:
            allowed_stp = {"EXPIRE_TAKER", "EXPIRE_MAKER", "EXPIRE_BOTH"}
            if order_type in CONDITIONAL_ORDER_TYPES:
                allowed_stp.add("NONE")
            if stp_mode not in allowed_stp:
                raise ValueError("Unsupported self-trade-prevention mode.")
            time_in_force = str(payload.get("timeInForce", "")).upper()
            if time_in_force and time_in_force not in {"GTC", "IOC", "GTD"}:
                raise ValueError(
                    "Self-trade prevention is effective only with GTC, IOC, or GTD."
                )
            payload["selfTradePreventionMode"] = stp_mode
        if order_type in CONDITIONAL_ORDER_TYPES and order_type != "TRAILING_STOP_MARKET":
            if safe_float(payload.get("triggerPrice")) <= 0:
                raise ValueError(f"{order_type} requires a positive trigger price.")
        if order_type == "TRAILING_STOP_MARKET":
            callback = safe_float(payload.get("callbackRate"))
            if not (0.1 <= callback <= 10.0):
                raise ValueError("Trailing callback rate must be between 0.1% and 10%.")
        for key in ('newClientOrderId', 'clientAlgoId'):
            client_id = str(payload.get(key) or '')
            if client_id and (len(client_id) > 36 or re.fullmatch(r"[.A-Z:/a-z0-9_-]+", client_id) is None):
                raise ValueError("Client order ID contains unsupported characters or exceeds 36 characters.")
        return payload

    def _validate_position_mode(
        self,
        payload: dict[str, Any],
        emergency: bool = False,
    ) -> None:
        if self.account_can_trade is False:
            raise ValueError(
                "Binance reports that Futures trading is disabled for this account or API key."
            )
        position_side = str(payload.get("positionSide", "BOTH")).upper()
        if self.hedge_mode is None:
            if emergency:
                return
            raise ValueError(
                "Binance position mode is still loading. Refresh the account before placing an order."
            )
        if self.hedge_mode and position_side == "BOTH":
            raise ValueError("Hedge Mode requires positionSide LONG or SHORT.")
        if not self.hedge_mode and position_side != "BOTH":
            raise ValueError("One-way Mode requires positionSide BOTH.")

    @staticmethod
    def _settings_error(message: str) -> str:
        explanations = {
            -4047: "Cross margin cannot be enabled while the symbol has open orders.",
            -4048: "Cross margin cannot be enabled while the symbol has an open position.",
            -4028: "Binance rejected the requested leverage for this symbol.",
            -2027: "The requested leverage is too high for the position notional bracket.",
            -2028: "The requested leverage is too low for the current position notional.",
            -2015: "The API key, IP restriction, or Futures trading permission is invalid.",
        }
        explanation = explanations.get(binance_error_code(message))
        return f"{explanation} Binance response: {message}" if explanation else message

    def _remember_failure(
        self,
        request_id: str,
        message: str,
        uncertain: bool,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if request_id in self.terminal_requests:
            return
        self.admitted_requests.discard(request_id)
        details = dict(metadata or self.request_details.get(request_id, {}))
        if not uncertain:
            self._collateral_reservations.pop(request_id, None)
        context = details.get("context")
        client_id = str(details.get('client_id') or '')
        record = self._protection_records.get(client_id)
        if record is not None and not uncertain:
            obsolete = isinstance(context, dict) and context.get('protection_obsolete')
            record['last_status'] = 'CANCELED' if obsolete else 'REJECTED'
            if obsolete:
                record['context']['protection_obsolete'] = True
            record['_status_received_mono'] = time.monotonic()
            if not (record.get('execution', {}).get('orderId') or record.get('execution', {}).get('algoId')):
                record['not_accepted'] = True
            self._protection_cleanup_clients.discard(client_id)
            self._persist_protection_record(record)
        if isinstance(context, dict):
            self.failure_contexts[request_id] = dict(context)
        fingerprint = str(details.get("fingerprint", ""))
        if fingerprint:
            self.inflight_fingerprints.pop(fingerprint, None)
            if uncertain:
                self.unresolved_fingerprints[fingerprint] = request_id
            else:
                self.unresolved_fingerprints.pop(fingerprint, None)
        if not uncertain:
            self._pop_request_details(request_id, None)
            self._mark_terminal(request_id)
        if isinstance(context, dict) and context.get('cleanup_client'):
            self._protection_recovery_pending = True
            self._queue_account_refresh(str(context.get('symbol') or ''))
        elif isinstance(context, dict) and context.get('protection_obsolete'):
            self._queue_account_refresh(str(context.get('symbol') or ''))
        self.request_failed.emit(request_id, message, uncertain)

    def _finish_request(self, request_id: str, result: Any) -> None:
        if request_id in self.terminal_requests:
            return
        details = self.request_details.get(request_id, {})
        method = str(details.get('method') or '')
        if not isinstance(result, dict):
            self._handle_transport_failure(request_id, details, 'Unexpected response: Binance order confirmation is not an object.')
            return
        result = normalize_order(result)
        if method == 'algoOrder.place' and result.get('status') == 'FINISHED' and not result.get('_child_reconciled'):
            result['_awaiting_child'] = True
        if method in {'order.place', 'algoOrder.place', 'order.modify', 'order.cancel', 'algoOrder.cancel'}:
            algo = method.startswith('algoOrder.')
            id_key = 'algoId' if algo else 'orderId'
            if safe_float(result.get(id_key)) <= 0:
                self._handle_transport_failure(request_id, details, 'Unexpected response: Binance order confirmation omitted its order identifier.')
                return
            if method.endswith('.place') and not (result.get('clientAlgoId') if algo else result.get('clientOrderId')):
                self._handle_transport_failure(request_id, details, 'Unexpected response: Binance order confirmation omitted its client identifier.')
                return
            wanted_client = str(details.get('client_id') or (details.get('params') or {}).get('clientAlgoId')
                                or (details.get('params') or {}).get('origClientOrderId') or '')
            returned_client = str(result.get('clientAlgoId') or result.get('clientOrderId') or '')
            if wanted_client and returned_client != wanted_client:
                self._handle_transport_failure(request_id, details, 'Unexpected response: Binance order confirmation did not verify the requested client identifier.')
                return
            if not valid_order_fills(result):
                self._handle_transport_failure(request_id, details, 'Unexpected response: Binance returned invalid order fill data.')
                return
        details = self._pop_request_details(request_id, {})
        fingerprint = str(details.get("fingerprint", ""))
        if fingerprint:
            self.inflight_fingerprints.pop(fingerprint, None)
            self.unresolved_fingerprints.pop(fingerprint, None)
        self.reconciling.discard(request_id)
        expected = details.get("expected") or details.get('params') or {}
        if isinstance(result, dict):
            expected_client = str(details.get('client_id') or expected.get('origClientOrderId') or expected.get('clientAlgoId') or '')
            mismatches = self._response_mismatches(expected, result, expected_client)
            if mismatches:
                self._set_request_details(request_id, details)
                self.problem.emit(
                    "BINANCE RESPONSE VERIFICATION FAILED · " + "; ".join(mismatches)
                )
                self._remember_failure(
                    request_id,
                    "Binance returned fields that do not match the transmitted order. The outcome requires manual review.",
                    True,
                    details,
                )
                return
            result = {
                **result,
                "_context": details.get("context", result.get("_context", {})),
            }
        self._mark_terminal(request_id)
        reservation = self._collateral_reservations.get(request_id)
        if reservation is not None:
            reservation['accepted_at'] = time.monotonic()
        client_id = str(details.get('client_id') or '')
        record = self._protection_records.get(client_id)
        if record is not None and isinstance(result, dict):
            self._observe_protection_order(client_id, result)
        cleanup_client = str(details.get('context', {}).get('cleanup_client') or '')
        if cleanup_client:
            cleanup_status = result.get('status') or ('CANCELED' if str(result.get('code')) == '200' else '')
            self._observe_protection_order(cleanup_client, {**result, 'status': cleanup_status})
        if result.get('_awaiting_child') and self._protection_records:
            QTimer.singleShot(0, self, self._recover_protections)
        self.request_succeeded.emit(request_id, result)

    @staticmethod
    def _response_mismatches(expected: dict[str, Any], result: dict[str, Any], client_id: str = "") -> list[str]:
        mismatches = []
        fields = {
            'orderId': ('orderId',), 'algoId': ('algoId',),
            "symbol": ("symbol",), "side": ("side",), "type": ("type", "orderType"),
            "quantity": ("origQty", "quantity"), "price": ("price",),
            "triggerPrice": ("triggerPrice", "stopPrice"), "activatePrice": ("activatePrice", "activationPrice"),
            "callbackRate": ("callbackRate", "priceRate"), "positionSide": ("positionSide",),
            "reduceOnly": ("reduceOnly",), "closePosition": ("closePosition",), "priceProtect": ("priceProtect",),
            "workingType": ("workingType",), "timeInForce": ("timeInForce",),
            "priceMatch": ("priceMatch",), "selfTradePreventionMode": ("selfTradePreventionMode",),
            "goodTillDate": ("goodTillDate",),
        }
        numeric = {"quantity", "price", "triggerPrice", "activatePrice", "callbackRate", "goodTillDate", 'orderId', 'algoId'}
        booleans = {"reduceOnly", "closePosition", "priceProtect"}
        for key, aliases in fields.items():
            if expected.get(key) in (None, ""):
                continue
            returned_key = next((name for name in aliases if result.get(name) not in (None, "")), None)
            if returned_key is None:
                continue
            wanted, actual = str(expected[key]), str(result[returned_key])
            if key in numeric:
                try:
                    left, right = Decimal(wanted), Decimal(actual)
                    matches = left.is_finite() and right.is_finite() and left == right
                except InvalidOperation:
                    matches = False
            elif key in booleans:
                matches = wanted.lower() in {"true", "false"} and wanted.lower() == actual.lower()
            else:
                matches = wanted.upper() == actual.upper()
            if not matches:
                mismatches.append(f"{key} {actual} != {wanted}")
        returned_client = str(result.get("clientOrderId") or result.get("clientAlgoId") or "")
        if client_id and returned_client and client_id != returned_client:
            mismatches.append("client order ID mismatch")
        return mismatches

    def _batch_response_matches(self, details: dict[str, Any], rows: list[dict[str, Any]]) -> bool:
        by_client = {str(row.get("clientOrderId") or row.get("clientAlgoId") or ""): row for row in rows if isinstance(row, dict)}
        expected_orders = details.get("expected_orders", [])
        if len(by_client) != len(expected_orders):
            return False
        for expected in expected_orders:
            client_id = str(expected.get("newClientOrderId") or expected.get("clientAlgoId") or "")
            result = by_client.get(client_id)
            if (result is None or not valid_order_fills(normalize_order(result))
                    or safe_float(result.get('algoId') or result.get('orderId')) <= 0
                    or self._response_mismatches(expected, result, client_id)):
                return False
        return True

    def _open_trade_socket(self) -> None:
        if self.stopping or not self.has_credentials():
            return
        if self.trade_socket.state() in (
            QtNetwork.QAbstractSocket.SocketState.ConnectedState,
            QtNetwork.QAbstractSocket.SocketState.ConnectingState,
        ):
            return
        delay = BINANCE_RATE_LIMITER.reserve_websocket_connection()
        if delay > 0:
            QTimer.singleShot(round(delay * 1_000), self._open_trade_socket)
            return
        self.trade_socket.open(QUrl(TEST_TRADE_WS if self.testnet else MAIN_TRADE_WS))

    def _trade_connected(self) -> None:
        self.trade_connected = True
        self.state_changed.emit("ARMED · ORDER LINK LIVE")

    def _trade_error(self, _error: object) -> None:
        self.state_changed.emit("ORDER LINK OFFLINE")
        if (
            not self.stopping
            and self.has_credentials()
            and self.trade_socket.state()
            == QtNetwork.QAbstractSocket.SocketState.UnconnectedState
            and not self.trade_reconnect.isActive()
        ):
            self.trade_reconnect.start()

    def _trade_disconnected(self) -> None:
        self.trade_connected = False
        if self.pending:
            pending = list(self.pending.items())
            self.pending.clear()
            self.pending_timeout_timer.stop()
            for request_id, metadata in pending:
                self.admitted_requests.discard(request_id)
                self._handle_transport_failure(
                    request_id,
                    metadata,
                    "The order link closed before Binance confirmed the outcome.",
                )
        if not self.stopping and self.has_credentials():
            self.state_changed.emit("ARMED · RECONNECTING ORDER LINK")
            self.trade_reconnect.start()

    def _queue_account_refresh(self, symbol: str = "") -> None:
        symbol = str(symbol).upper().strip()
        if symbol:
            self.account_event_refresh_symbols.add(symbol)
        if not self.stopping and self.has_credentials():
            self.account_event_refresh_timer.start()

    def _refresh_after_account_event(self) -> None:
        symbols = set(self.account_event_refresh_symbols)
        self.account_event_refresh_symbols.clear()
        symbol = next(iter(symbols)) if len(symbols) == 1 else None


        self.refresh_account(
            symbol,
            all_open_orders=symbol is None,
            follow_up=True,
        )

    def _finish_uncertain_nonplacement(
        self,
        request_id: str,
        metadata: dict[str, Any],
        message: str,
    ) -> None:
        self._remember_failure(request_id, message, True, metadata)
        self._pop_request_details(request_id, None)
        self._mark_terminal(request_id)
        expected = metadata.get("expected") or metadata.get("params") or {}
        self._queue_account_refresh(str(expected.get("symbol", "")))

    def _handle_transport_failure(
        self,
        request_id: str,
        metadata: dict[str, Any],
        message: str,
    ) -> None:
        uncertain = execution_outcome_uncertain(message)
        metadata['transport_state'] = 'uncertain' if uncertain else 'rejected'
        method = str(metadata.get("method", ""))
        if uncertain and method in {"order.place", "algoOrder.place"}:
            self._begin_reconciliation(request_id, metadata, message)
        elif uncertain and method in {'order.modify', 'order.cancel', 'algoOrder.cancel'}:
            self._begin_operation_reconciliation(request_id, metadata, message)
        elif uncertain:
            self._finish_uncertain_nonplacement(request_id, metadata, message)
        else:
            self._remember_failure(request_id, message, False, metadata)

    def _begin_operation_reconciliation(self, request_id: str, metadata: dict, reason: str) -> None:
        """Query an uncertain amendment/cancel without transmitting it again."""
        if request_id in self.terminal_requests or request_id in self.reconciling:
            return
        details = {**self.request_details.get(request_id, {}), **metadata, 'transport_state': 'uncertain'}
        params = dict(details.get('params') or details.get('expected') or {})
        method = str(details.get('method') or '')
        self._set_request_details(request_id, details)
        self.reconciling.add(request_id)
        api_key, api_secret = self.api_key, self.api_secret

        def query():
            last_error = reason
            for attempt in range(6):
                if attempt:
                    time.sleep(0.8)
                try:
                    row = normalize_order(self.rest.query_order_request(api_key, api_secret, params, algo=method == 'algoOrder.cancel'))
                    if not valid_order_fills(row):
                        raise RuntimeError('Unexpected response: Binance returned invalid order fill data.')
                    identifier = row.get('algoId') if method == 'algoOrder.cancel' else row.get('orderId')
                    if safe_float(identifier) <= 0:
                        raise RuntimeError('Unexpected response: order query omitted its identifier.')
                    terminal = str(row.get('status') or '') in {'CANCELED', 'FILLED', 'FINISHED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'REJECTED'}
                    if method == 'order.modify':
                        if terminal or not self._response_mismatches(params, row):
                            return row
                    elif terminal:
                        return row
                    last_error = 'The queried order has not confirmed the requested change.'
                except (RuntimeError, TimeoutError, OSError) as exc:
                    last_error = str(exc)
            raise RuntimeError(last_error)

        def done(row):
            self.reconciling.discard(request_id)
            if method == 'order.modify' and self._response_mismatches(params, row):
                self._remember_failure(request_id, f"Amendment outcome reconciled: the order is {row.get('status')}; the requested changes were not established.", False, details)
            else:
                self._finish_request(request_id, {**row, '_transport': 'RECONCILED BY ORDER ID', '_reconciled': True})
            self._queue_account_refresh(str(params.get('symbol') or ''))

        def failed(message):
            self.reconciling.discard(request_id)
            self._remember_failure(request_id, f'{reason} Read-only reconciliation did not establish the change: {message}', True, details)
            self._queue_account_refresh(str(params.get('symbol') or ''))

        task = self._launch_task(query, done, failed, self.task_pool)
        self.tasks.add(task)

    def _expire_pending_requests(self) -> None:
        now = time.monotonic()
        expired = [
            (request_id, metadata)
            for request_id, metadata in self.pending.items()
            if now - safe_float(metadata.get("sent"), now) >= 12.0
        ]
        for request_id, metadata in expired:
            self.pending.pop(request_id, None)
            self.admitted_requests.discard(request_id)
            self._handle_transport_failure(
                request_id,
                metadata,
                "The Binance WebSocket request timed out; execution status is unknown.",
            )
        if not self.pending:
            self.pending_timeout_timer.stop()

    @staticmethod
    def _ws_signature(params: dict[str, Any], secret: str) -> str:
        encoded = BinanceRest._encoded_params(params)
        query = '&'.join(f'{key}={value}' for key, value in sorted(encoded.items()))
        return hmac.new(
            secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _begin_reconciliation(
        self,
        request_id: str,
        metadata: dict[str, Any],
        reason: str,
    ) -> None:
        if request_id in self.terminal_requests:
            return
        details = {**self.request_details.get(request_id, {}), **metadata}
        self._set_request_details(request_id, details)
        client_id = str(details.get("client_id", ""))
        symbol = str((details.get("expected") or {}).get("symbol", ""))
        method = str(details.get("method", ""))
        if not client_id or not symbol or method not in {"order.place", "algoOrder.place"}:
            self._remember_failure(
                request_id,
                reason + " No safe client-ID reconciliation is available for this request.",
                True,
                details,
            )
            return
        if request_id in self.reconciling:
            return
        self.reconciling.add(request_id)
        fingerprint = str(details.get("fingerprint", ""))
        if fingerprint:
            self.inflight_fingerprints.pop(fingerprint, None)
            self.unresolved_fingerprints[fingerprint] = request_id
        self.state_changed.emit(f"RECONCILING · {client_id}")
        task: ApiTask
        api_key, api_secret = self.api_key, self.api_secret

        def query() -> dict[str, Any]:
            last_error = reason
            for attempt in range(6):
                if attempt:
                    time.sleep(0.8)
                try:
                    result = normalize_order(self.rest.query_order_by_client_id(
                        api_key,
                        api_secret,
                        symbol,
                        client_id,
                        algo=method == "algoOrder.place",
                    ))
                    returned_client = str(result.get('clientAlgoId') or result.get('clientOrderId') or '')
                    if (safe_float(result.get('algoId') or result.get('orderId')) <= 0 or returned_client != client_id
                            or self._response_mismatches(details.get('expected') or {}, result, client_id)):
                        raise RuntimeError('Unexpected response: queried order could not be verified against its saved intent.')
                    return self._with_child_execution(api_key, api_secret, symbol, result)
                except (RuntimeError, TimeoutError, OSError) as exc:
                    last_error = str(exc)
                    if not any(
                        token in last_error.lower()
                        for token in (
                            "does not exist", "unknown order", "-2013", "not found",
                            "timeout", "timed out", "network error",
                        )
                    ) and re.search(r"http 5\d\d", last_error.lower()) is None:
                        break
            raise RuntimeError(last_error)

        def done(result: dict[str, Any]) -> None:
            self.tasks.discard(task)
            self.reconciling.discard(request_id)
            result = {
                **dict(result),
                "_transport": "RECONCILED BY CLIENT ID",
                "_reconciled": True,
            }
            self._finish_request(request_id, result)

        def failed(message: str) -> None:
            self.tasks.discard(task)
            self.reconciling.discard(request_id)
            self._remember_failure(
                request_id,
                f"{reason} Client-ID reconciliation did not establish a final outcome: {message}",
                True,
                details,
            )

        task = self._launch_task(query, done, failed, self.task_pool)
        self.tasks.add(task)

    def reconcile_unknown_orders(self) -> None:
        if self._protection_cleanup_clients:
            for client_id in self._protection_cleanup_clients:
                self._protection_cleanup_attempts.pop(client_id, None)
        if self._protection_records and (self._protection_recovery_pending or self._protection_cleanup_clients):
            self._recover_protections()
        candidates = [
            (request_id, dict(details))
            for request_id, details in self.request_details.items()
            if str(details.get("fingerprint", "")) in self.unresolved_fingerprints
            or (details.get('transport_state') == 'uncertain' and details.get('method') in {'order.modify', 'order.cancel', 'algoOrder.cancel'})
        ]
        if not candidates and not self._protection_recovery_pending:
            self.problem.emit("No unresolved order placements are waiting for reconciliation.")
            return
        for request_id, details in candidates:
            if str(details.get("method", "")) == "batchOrders.place":
                self._begin_batch_reconciliation(
                    request_id,
                    details,
                    "Manual batch reconciliation requested.",
                )
            elif details.get('method') in {'order.modify', 'order.cancel', 'algoOrder.cancel'}:
                self._begin_operation_reconciliation(request_id, details, 'Manual operation reconciliation requested.')
            else:
                self._begin_reconciliation(
                    request_id,
                    details,
                    "Manual reconciliation requested.",
                )

    def submit_order(self, order: dict[str, Any], context: dict[str, Any] | None = None) -> str:
        request_id = self._next_id("order")
        context = dict(context or {})
        context['_server_time_ms'] = self.rest.cached_timestamp_ms()
        emergency = bool(context.get("emergency_close"))
        protective = bool(context.get('protective_leg'))
        reducing = emergency or protective or str(context.get('position_intent') or '').upper() == 'REDUCE' or str(order.get('reduceOnly') or '').lower() == 'true'
        if self.stopping or not self.has_credentials():
            self._remember_failure(request_id, "API credentials are required; trading session unavailable.", False,
                                   {"context": context})
            return request_id
        if not reducing and (self._protection_recovery_pending or self._protection_cleanup_clients or self.account_can_trade is not True):
            self._remember_failure(request_id, 'New entries require confirmed account permissions and completed protection recovery.', False, {'context': context})
            return request_id
        if context.get('protections') and (context['protections'].get('tp') or context['protections'].get('sl')) and not reducing:
            if self.user_socket.state() != QtNetwork.QAbstractSocket.SocketState.ConnectedState:
                self.connect_session()
                self._remember_failure(request_id, 'An entry with TP/SL requires a connected account stream; wait for ACCOUNT STREAM LIVE.', False, {'context': context})
                return request_id
        if context.get("requires_arm") and not self.armed and not emergency:
            self._remember_failure(
                request_id,
                "Trading is disarmed.",
                False,
                {"context": context},
            )
            return request_id
        limit = self.MAX_PROTECTIVE_WRITES if protective else self.MAX_OPEN_ORDER_WRITES if reducing else self.MAX_ENTRY_WRITES
        used = self._open_order_write_count(protective=protective)
        if not emergency and used >= limit:
            self._remember_failure(
                request_id,
                "Too many order placements are still awaiting a final Binance outcome. "
                "Wait or reconcile them before sending more.",
                False,
                {"context": context},
            )
            return request_id
        try:
            payload = self._validate_order_payload(order, context)
            self._validate_position_mode(payload, emergency)
        except ValueError as exc:
            self._remember_failure(
                request_id, f"Order validation failed: {exc}", False, {"context": context}
            )
            return request_id
        symbol = payload['symbol']
        if symbol and symbol in self.cross_pending and not reducing:
            self._remember_failure(
                request_id,
                "Margin mode or leverage is still being applied for this symbol. Wait for CROSS READY, then submit again.",
                False,
                {"context": context},
            )
            return request_id
        if symbol and symbol not in self.cross_ready and not reducing and not context.get("existing_order_replacement"):
            self.ensure_cross(symbol)
            self._remember_failure(
                request_id,
                "Cross margin is still being verified for this symbol. Wait for CROSS READY, then submit again.",
                False,
                {"context": context},
            )
            return request_id
        fingerprint = self._order_fingerprint(payload)
        if protective:
            # Separate fill tranches may have equal prices and lot sizes. The
            # durable, unique leg client ID is their idempotency key.
            fingerprint = hashlib.sha256((fingerprint + ':' + str(context.get('client_id'))).encode()).hexdigest()
        if fingerprint in self.inflight_fingerprints or fingerprint in self.unresolved_fingerprints:
            self._remember_failure(
                request_id,
                "An identical order intent is already in flight or has an unresolved Binance outcome. Reconcile it before submitting again.",
                False,
                {"context": context},
            )
            return request_id
        payload.setdefault("newClientOrderId", self.client_order_id())
        order_type = str(payload.get("type", "")).upper()
        if order_type in CONDITIONAL_ORDER_TYPES:
            payload.setdefault("algoType", "CONDITIONAL")
            payload.setdefault("clientAlgoId", payload.pop("newClientOrderId"))
            method = "algoOrder.place"
        else:
            payload.setdefault("newOrderRespType", "RESULT")
            method = "order.place"
        client_id = str(payload.get("clientAlgoId") or payload.get("newClientOrderId") or "")
        if protective:
            token = self._protection_cancel_tokens.setdefault(client_id, threading.Event())
            try:
                if self.remaining_protection_quantity(context) <= 0:
                    token.set()
            except (ValueError, ArithmeticError) as exc:
                self._remember_failure(request_id, f'Protection quantity could not be verified: {exc}', False, {'context': context})
                return request_id
        details = {
            "method": method,
            "context": context,
            "expected": dict(payload),
            "client_id": client_id,
            "fingerprint": fingerprint,
            "sent": 0.0,
        }
        required = safe_float(context.get('collateral_required'))
        asset = str(context.get('collateral_asset') or '')
        if required > 0 and not reducing:
            try:
                available = self.available_balance(asset)
            except ValueError as exc:
                self._remember_failure(request_id, str(exc), False, details)
                return request_id
            if required > available + 1e-9:
                self._remember_failure(request_id, 'Collateral has already been reserved by another admitted order. Refresh or reduce allocation.', False, details)
                return request_id
            self._collateral_reservations[request_id] = {'asset': asset, 'amount': required, 'accepted_at': None}
        return self._send_or_rest(
            request_id,
            method,
            payload,
            context,
            details,
        )

    def submit_modify(
        self,
        changes: dict[str, Any],
        rules: SymbolRules | None = None,
    ) -> str:
        request_id = self._next_id("modify")
        if self.stopping or not self.has_credentials():
            self._remember_failure(request_id, "API credentials are required; trading session unavailable.", False)
            return request_id
        payload = dict(changes)
        minimum_executed = payload.pop("_minimumExecutedQty", None)
        try:
            symbol = str(payload.get("symbol", "")).upper().strip()
            side = str(payload.get("side", "")).upper().strip()
            if not re.fullmatch(r"[A-Z0-9]{5,24}", symbol):
                raise ValueError("Order symbol is missing or invalid.")
            if side not in {"BUY", "SELL"}:
                raise ValueError("Order side must be BUY or SELL.")
            if not any(
                payload.get(key) not in (None, "")
                for key in ("orderId", "origClientOrderId")
            ):
                raise ValueError("A Binance order identifier is required.")
            payload["symbol"] = symbol
            payload["side"] = side
            if 'reduceOnly' in payload:
                flag = exchange_bool(payload['reduceOnly'])
                if flag is None:
                    raise ValueError('reduceOnly must be true or false.')
                payload['reduceOnly'] = flag
            if isinstance(rules, SymbolRules):
                payload["quantity"] = validate_step(
                    str(payload.get("quantity", "")), rules.lot_step, "Quantity", offset=str(rules.min_qty)
                )
                payload["price"] = validate_step(
                    str(payload.get("price", "")), rules.tick_size, "Price", offset=str(rules.min_price)
                )
                quantity = safe_float(payload["quantity"])
                price = safe_float(payload["price"])
                if quantity <= 0 or not (rules.min_qty <= quantity <= rules.max_qty):
                    raise ValueError(
                        f"Quantity must be between {rules.min_qty:g} and {rules.max_qty:g}."
                    )
                if price <= 0 or price < rules.min_price or (rules.max_price > 0 and price > rules.max_price):
                    raise ValueError(
                        f"Price must be between {rules.min_price:g} and {rules.max_price:g}."
                    )
            elif safe_float(payload.get("quantity")) <= 0 or safe_float(
                payload.get("price")
            ) <= 0:
                raise ValueError("Modification quantity and price must be positive.")
            if minimum_executed is not None:
                executed = Decimal(str(minimum_executed))
                if not executed.is_finite() or executed < 0 or Decimal(str(payload["quantity"])) <= executed:
                    raise ValueError("New total quantity must exceed the already filled amount.")
        except (ValueError, InvalidOperation) as exc:
            self._remember_failure(
                request_id,
                f"Order modification validation failed: {exc}",
                False,
                {"context": {"rules": rules}},
            )
            return request_id
        return self._send_or_rest(
            request_id,
            "order.modify",
            payload,
            {"rules": rules},
        )

    def submit_batch_orders(
        self,
        orders: list[dict[str, Any]],
        rules: SymbolRules | None = None,
        position_intent: str = "OPEN",
        requires_arm: bool = False,
    ) -> str:
        request_id = self._next_id("batch")
        position_intent = str(position_intent).upper()
        if position_intent not in {"OPEN", "REDUCE"}:
            position_intent = "OPEN"
        context: dict[str, Any] = {
            "batch": True,
            "rules": rules,
            "position_intent": position_intent,
            '_server_time_ms': self.rest.cached_timestamp_ms(),
        }
        if position_intent == 'OPEN' and (self._protection_recovery_pending or self._protection_cleanup_clients or self.account_can_trade is not True):
            self._remember_failure(request_id, 'New entries require confirmed account permissions and completed protection recovery.', False, {'context': context})
            return request_id
        if self.stopping or not self.has_credentials():
            self._remember_failure(request_id, "API credentials are required; trading session unavailable.", False,
                                   {"context": context})
            return request_id
        if requires_arm and not self.armed:
            self._remember_failure(
                request_id, "Trading is disarmed.", False, {"context": context}
            )
            return request_id
        if not orders:
            self._remember_failure(
                request_id, "The batch contains no orders.", False, {"context": context}
            )
            return request_id
        if len({str(order.get('symbol') or '').upper() for order in orders}) != 1:
            self._remember_failure(request_id, 'Each batch must use one symbol and its exchange rules; nothing was sent.', False, {'context': context})
            return request_id
        if len(orders) > 5:
            self._remember_failure(
                request_id,
                "Binance USD-M batches accept at most five orders; nothing was sent.",
                False,
                {"context": context},
            )
            return request_id
        limit = self.MAX_ENTRY_WRITES if position_intent == 'OPEN' else self.MAX_OPEN_ORDER_WRITES
        if self._open_order_write_count(protective=False) + len(orders) > limit:
            self._remember_failure(
                request_id,
                "This batch would exceed Nightwatch's in-flight order safety limit. "
                "Wait for current placements to finish first.",
                False,
                {"context": context},
            )
            return request_id
        prepared: list[dict[str, Any]] = []
        try:
            for order in orders:
                item = self._validate_order_payload(
                    order,
                    context,
                )
                self._validate_position_mode(item)
                item.setdefault("newClientOrderId", self.client_order_id("nwb"))
                if item['type'] in CONDITIONAL_ORDER_TYPES:
                    item.setdefault('algoType', 'CONDITIONAL')
                    item.setdefault('clientAlgoId', item.pop('newClientOrderId'))
                prepared.append(item)
            clients = [str(item.get('clientAlgoId') or item.get('newClientOrderId')) for item in prepared]
            if len(set(clients)) != len(clients):
                raise ValueError('Every order in a batch requires a different client identifier.')
        except ValueError as exc:
            self._remember_failure(
                request_id,
                f"Batch validation failed: {exc}",
                False,
                {"context": context},
            )
            return request_id
        symbol = prepared[0]['symbol']
        if symbol in self.cross_pending and position_intent == 'OPEN':
            self._remember_failure(
                request_id,
                "Margin mode or leverage is still being applied. Wait for CROSS READY, then submit the batch again.",
                False,
                {"context": context},
            )
            return request_id
        if symbol not in self.cross_ready and position_intent == 'OPEN':
            self.ensure_cross(symbol)
            self._remember_failure(
                request_id,
                "Cross margin is still being verified. Wait for CROSS READY, then submit the batch again.",
                False,
                {"context": context},
            )
            return request_id
        fingerprint = hashlib.sha256(
            "|".join(self._order_fingerprint(order) for order in prepared).encode("utf-8")
        ).hexdigest()
        if fingerprint in self.inflight_fingerprints or fingerprint in self.unresolved_fingerprints:
            self._remember_failure(
                request_id,
                "An identical batch is already in flight or has an unresolved Binance outcome. Reconcile it before submitting again.",
                False,
                {"context": context},
            )
            return request_id
        details = {
            "method": "batchOrders.place",
            "context": context,
            "expected_orders": [dict(order) for order in prepared],
            "client_ids": [str(order.get('clientAlgoId') or order.get("newClientOrderId", "")) for order in prepared],
            "fingerprint": fingerprint,
            "sent": time.monotonic(),
        }
        task: ApiTask

        def done(result: Any) -> None:
            self.tasks.discard(task)
            self.admitted_requests.discard(request_id)
            if request_id in self.terminal_requests:
                return
            rows = [normalize_order(row) if isinstance(row, dict) else row for row in result] if isinstance(result, list) else []
            if len(rows) != len(prepared):
                self._begin_batch_reconciliation(
                    request_id,
                    details,
                    "Binance returned an incomplete batch response.",
                )
                return
            errors = [
                row
                for row in rows
                if isinstance(row, dict) and safe_float(row.get("code")) < 0
            ]
            if errors:
                accepted_rows = [row for row in rows if isinstance(row, dict) and safe_float(row.get('code')) >= 0]
                accepted_clients = {str(row.get('clientAlgoId') or row.get('clientOrderId') or '') for row in accepted_rows}
                accepted_intents = [order for order in prepared
                                    if str(order.get('clientAlgoId') or order.get('newClientOrderId') or '') in accepted_clients]
                if (len(accepted_rows) + len(errors) != len(prepared)
                        or len(accepted_clients) != len(accepted_rows) or len(accepted_intents) != len(accepted_rows)
                        or not self._batch_response_matches({'expected_orders': accepted_intents}, accepted_rows)):
                    self._begin_batch_reconciliation(request_id, details, 'Binance returned unverifiable results inside a partial batch.')
                    return
                accepted = len(accepted_rows)
                transport_uncertain = any(
                    bool(row.get('_uncertain')) or (not row.get('_notSent') and execution_outcome_uncertain(row))
                    for row in errors
                    if isinstance(row, dict)
                )
                first = errors[0]
                code = int(safe_float(first.get("code")))
                detail = str(first.get("msg") or first.get("message") or "rejected")
                self._remember_failure(
                    request_id,
                    f"Batch reported {accepted} accepted and {len(errors)} failed order(s). "
                    f"First rejection: {code} · {detail}. "
                    + (
                        "Do not resubmit the complete batch; review the accepted client IDs first."
                        if accepted else ('Acceptance of the failed orders remains unresolved.' if transport_uncertain else 'No order in the batch was accepted.')
                    ),
                    transport_uncertain,
                    details,
                )
                self._queue_account_refresh(symbol)
                return
            returned_ids = {
                str(row.get("clientOrderId") or row.get("clientAlgoId") or "")
                for row in rows
                if isinstance(row, dict)
            }
            missing_ids = [client_id for client_id in details["client_ids"] if client_id not in returned_ids]
            if missing_ids or not self._batch_response_matches(details, rows):
                reason = "Binance returned unverifiable execution fields inside the batch."
                self._begin_batch_reconciliation(request_id, details, reason)
                return
            self._pop_request_details(request_id, None)
            self.inflight_fingerprints.pop(fingerprint, None)
            self.unresolved_fingerprints.pop(fingerprint, None)
            self._mark_terminal(request_id)
            self.request_succeeded.emit(
                request_id,
                {
                    "orders": rows,
                    "_context": context,
                    "_transport": "REST BATCH",
                },
            )

        def failed(message: str) -> None:
            self.tasks.discard(task)
            self.admitted_requests.discard(request_id)
            if request_id in self.terminal_requests:
                return
            uncertain = execution_outcome_uncertain(message)
            contains_conditional = any(
                str(order.get("type", "")).upper() in CONDITIONAL_ORDER_TYPES
                for order in prepared
            )
            if uncertain or contains_conditional:
                self._begin_batch_reconciliation(request_id, details, message)
            else:
                self._remember_failure(request_id, message, False, details)

        def transmit() -> None:
            nonlocal task
            api_key, api_secret = self.api_key, self.api_secret
            task = self._launch_task(
                lambda: self.rest.place_batch_orders(api_key, api_secret, prepared),
                done, failed, self.task_pool,
            )
            self.tasks.add(task)

        self._queue_placement(request_id, details, transmit)
        return request_id

    def _begin_batch_reconciliation(
        self,
        request_id: str,
        metadata: dict[str, Any],
        reason: str,
    ) -> None:
        if request_id in self.terminal_requests:
            return
        details = {**self.request_details.get(request_id, {}), **metadata}
        self._set_request_details(request_id, details)
        orders = [dict(order) for order in details.get("expected_orders", [])]
        if not orders:
            self._remember_failure(
                request_id,
                reason + " No safe client-ID reconciliation data is available.",
                True,
                details,
            )
            return
        if request_id in self.reconciling:
            return
        self.reconciling.add(request_id)
        fingerprint = str(details.get("fingerprint", ""))
        if fingerprint:
            self.inflight_fingerprints.pop(fingerprint, None)
            self.unresolved_fingerprints[fingerprint] = request_id
        self.state_changed.emit(f"RECONCILING BATCH · {len(orders)} ORDERS")
        task: ApiTask
        api_key, api_secret = self.api_key, self.api_secret

        def query() -> list[dict[str, Any]]:
            resolved: list[dict[str, Any]] = []
            unresolved: list[str] = []
            for order in orders:
                client_id = str(order.get("newClientOrderId") or order.get("clientAlgoId") or "")
                symbol = str(order.get("symbol", ""))
                algo = str(order.get("type", "")).upper() in CONDITIONAL_ORDER_TYPES
                found: dict[str, Any] | None = None
                last_error = "unknown order"
                for attempt in range(6):
                    if attempt:
                        time.sleep(0.6)
                    try:
                        found = normalize_order(self.rest.query_order_by_client_id(
                            api_key,
                            api_secret,
                            symbol,
                            client_id,
                            algo=algo,
                        ))
                        break
                    except (RuntimeError, TimeoutError, OSError) as exc:
                        last_error = str(exc)
                if found is None:
                    unresolved.append(f"{client_id}: {last_error}")
                else:
                    resolved.append(found)
            if unresolved:
                raise RuntimeError("; ".join(unresolved))
            return resolved

        def done(result: list[dict[str, Any]]) -> None:
            self.tasks.discard(task)
            self.reconciling.discard(request_id)
            if request_id in self.terminal_requests:
                return
            if not self._batch_response_matches(details, result):
                self._remember_failure(request_id, "Reconciled batch fields differ from the submitted intent; manual review is required.", True, details)
                return
            self._pop_request_details(request_id, None)
            if fingerprint:
                self.inflight_fingerprints.pop(fingerprint, None)
                self.unresolved_fingerprints.pop(fingerprint, None)
            self._mark_terminal(request_id)
            self.request_succeeded.emit(
                request_id,
                {
                    "orders": result,
                    "_context": details.get("context", {}),
                    "_transport": "RECONCILED BY CLIENT ID",
                    "_reconciled": True,
                },
            )

        def failed(message: str) -> None:
            self.tasks.discard(task)
            self.reconciling.discard(request_id)
            if request_id in self.terminal_requests:
                return
            self._remember_failure(
                request_id,
                f"{reason} Client-ID reconciliation did not establish every batch outcome: {message}",
                True,
                details,
            )

        task = self._launch_task(query, done, failed, self.task_pool)
        self.tasks.add(task)

    def submit_cancel(self, request: dict[str, Any], algo: bool = False, *, context: dict | None = None) -> str:
        request_id = self._next_id("cancel")
        if self.stopping or not self.has_credentials():
            self._remember_failure(request_id, "API credentials are required; trading session unavailable.", False)
            return request_id
        payload = dict(request)
        symbol = str(payload.get("symbol", "")).upper().strip()
        payload["symbol"] = symbol
        identifier_keys = (
            ("algoId", "clientAlgoId")
            if algo
            else ("orderId", "origClientOrderId")
        )
        if not re.fullmatch(r"[A-Z0-9]{5,24}", symbol):
            self._remember_failure(
                request_id, "Cancel symbol is missing or invalid.", False
            )
            return request_id
        if not any(payload.get(key) not in (None, "") for key in identifier_keys):
            self._remember_failure(
                request_id,
                "Cancel request is missing its Binance order identifier.",
                False,
            )
            return request_id
        return self._send_or_rest(
            request_id,
            "algoOrder.cancel" if algo else "order.cancel",
            payload,
            {**(context or {}), 'symbol': symbol},
        )

    def _send_or_rest(
        self,
        request_id: str,
        method: str,
        params: dict[str, Any],
        context: dict[str, Any],
        details: dict[str, Any] | None = None,
    ) -> str:
        metadata = {
            "method": method,
            "context": context,
            "params": dict(params),
            "sent": time.monotonic(),
            **(details or {}),
        }
        client_id = str(metadata.get("client_id") or "")
        if client_id and self._client_requests.get(client_id, request_id) != request_id:
            self._remember_failure(request_id, "Client order ID already has an unresolved request.", False, metadata)
            return request_id
        if method in {"order.place", "algoOrder.place"} and not metadata.get("_journaled"):
            self._queue_placement(
                request_id, metadata,
                lambda: self._send_or_rest(request_id, method, params, context, metadata),
            )
            return request_id
        token = self._protection_cancel_tokens.get(client_id) if context.get('protective_leg') else None

        def before_send() -> None:
            if token is not None and token.is_set():
                raise RuntimeError('Not sent: protection tranche already exited or closed.')

        try:
            before_send()
        except RuntimeError as exc:
            metadata['context'] = {**context, 'protection_obsolete': True}
            self._remember_failure(request_id, str(exc), False, metadata)
            return request_id
        plans = context.get('protections') or {}
        if (method in {'order.place', 'algoOrder.place'} and (plans.get('tp') or plans.get('sl'))
                and self.user_socket.state() != QtNetwork.QAbstractSocket.SocketState.ConnectedState):
            self._remember_failure(request_id, 'Not sent: account stream disconnected before the protected entry was transmitted.', False, metadata)
            return request_id
        wire_params = ({key: value for key, value in params.items() if key in {'algoId', 'clientAlgoId'}}
                       if method == 'algoOrder.cancel' else dict(params))
        ws_compatible = method != 'algoOrder.place' or str(params.get('timeInForce') or 'GTC') in {'GTC', 'IOC', 'FOK'}
        if (self.trade_connected and ws_compatible and self.rest.has_fresh_time_offset()
                and self.trade_socket.state() == QtNetwork.QAbstractSocket.SocketState.ConnectedState):

            try:
                signed = BinanceRest._encoded_params(wire_params)
                for key in ('orderId', 'algoId', 'goodTillDate', 'modifyId', 'recvWindow'):
                    if key in signed:
                        signed[key] = int(signed[key])
                signed.update(apiKey=self.api_key, timestamp=self.rest.cached_timestamp_ms(), recvWindow=5000)
                signed['signature'] = self._ws_signature(signed, self.api_secret)
                message = json.dumps({'id': request_id, 'method': method, 'params': signed}, separators=(',', ':'))
            except (RuntimeError, ValueError, TypeError) as exc:
                metadata['transport_state'] = 'not_sent'
                self._remember_failure(request_id, f'Not sent: {exc}', False, metadata)
                return request_id
            try:
                BINANCE_RATE_LIMITER.acquire(
                    0, 1 if method in {'order.place', 'algoOrder.place', 'order.modify'} else 0,
                    ('WS_REQUEST_WEIGHT',), 'manual', wait=False,
                    ws_request_weight=1 if method.endswith('.cancel') else 0,
                    source='order execution', endpoint=method,
                )
            except LocalRateLimitError as exc:
                metadata['transport_state'] = 'rate_limited'
                self._remember_failure(request_id, f'Local rate limit; not sent: {exc}', False, metadata)
                return request_id
            try:
                sent = self.trade_socket.sendTextMessage(message)
            except (RuntimeError, OSError) as exc:

                metadata['transport_state'] = 'uncertain'
                self._set_request_details(request_id, metadata)
                self._handle_transport_failure(request_id, metadata,
                    f'Execution status unknown after socket write: {exc}')
                return request_id
            if sent > 0:
                self.admitted_requests.add(request_id)
                metadata['transport_state'] = 'sent'
                metadata['sent'] = time.monotonic()
                self.pending[request_id] = metadata
                self._set_request_details(request_id, metadata)
                if not self.pending_timeout_timer.isActive():
                    self.pending_timeout_timer.start()
                fingerprint = str(metadata.get('fingerprint', ''))
                if fingerprint:
                    self.inflight_fingerprints[fingerprint] = request_id
                self.state_changed.emit(f'SENT · {method}')
                return request_id


        metadata['transport_state'] = 'not_sent'


        self.state_changed.emit(
            "TIME SYNC · USING REST ONCE"
            if self.trade_connected
            else "ORDER LINK STARTING · USING REST ONCE"
        )
        task: ApiTask
        api_key, api_secret = self.api_key, self.api_secret

        def execute() -> Any:
            if method in {"order.place", "algoOrder.place"}:
                return self.rest.place_order(api_key, api_secret, params, before_send=before_send)
            if method == "order.modify":
                return self.rest.modify_order(api_key, api_secret, params)
            return self.rest.cancel_order(
                api_key,
                api_secret,
                wire_params,
                algo=method == "algoOrder.cancel",
            )

        def done(result: Any) -> None:
            self.tasks.discard(task)
            self.admitted_requests.discard(request_id)
            if isinstance(result, dict):
                result = {**result, "_transport": "REST"}
            self._finish_request(request_id, result)

        def failed(message: str) -> None:
            self.tasks.discard(task)
            self.admitted_requests.discard(request_id)
            if message.startswith('Not sent: protection tranche already exited or closed.'):
                metadata['context'] = {**context, 'protection_obsolete': True}
            if 'rate window' in message.lower() or 'published rate limit' in message.lower():
                metadata['transport_state'] = 'rate_limited'
                self._remember_failure(request_id, 'Local rate limit; not sent: ' + message, False, metadata)
            else:
                self._handle_transport_failure(request_id, metadata, message)

        pool = self.protection_pool if context.get('protective_leg') or context.get('emergency_close') else self.task_pool
        task = self._launch_task(execute, done, failed, pool)
        self.admitted_requests.add(request_id)
        metadata["sent"] = time.monotonic()
        self._set_request_details(request_id, metadata)
        fingerprint = str(metadata.get("fingerprint", ""))
        if fingerprint:
            self.inflight_fingerprints[fingerprint] = request_id
        self.tasks.add(task)
        return request_id

    def _trade_message(self, message: str) -> None:
        if self.stopping:
            return
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        request_id = str(payload.get("id", ""))
        rate_limits = payload.get("rateLimits")
        if isinstance(rate_limits, list):
            BINANCE_RATE_LIMITER.observe_websocket_limits(rate_limits)
        metadata = self.pending.pop(request_id, {}) or self.request_details.get(
            request_id, {}
        )
        if not self.pending:
            self.pending_timeout_timer.stop()
        if not metadata and request_id not in self.request_details:
            return
        self.admitted_requests.discard(request_id)
        status = int(safe_float(payload.get("status"), 0))
        if payload.get("error") or status >= 400:
            error = payload.get("error") or {}
            detail = error.get("msg") if isinstance(error, dict) else str(error)
            error_code = int(safe_float(error.get("code"), 0)) if isinstance(error, dict) else 0
            if error_code == -1021 or "timestamp for this request" in str(detail).lower():
                self.rest.invalidate_time_sync()
            formatted = (
                f"{'HTTP ' + str(status) + ' · ' if status else ''}Binance {error_code}: {detail}"
                if error_code
                else detail or f"Binance returned HTTP {status}."
            )
            self._handle_transport_failure(request_id, metadata, formatted)
            return
        result = payload.get("result", payload)
        if isinstance(result, dict):
            result = {
                **result,
                "_transport": "WEBSOCKET",
            }
        self._finish_request(request_id, result)

    @staticmethod
    def _scope_satisfies(
        cached_scope: tuple[str | None, bool],
        requested_scope: tuple[str | None, bool],
    ) -> bool:
        cached_symbol, cached_all_orders = cached_scope
        requested_symbol, requested_all_orders = requested_scope
        if requested_all_orders and not cached_all_orders:
            return False
        return cached_symbol == requested_symbol

    def _snapshot_scope_is_fresh(
        self,
        requested_scope: tuple[str | None, bool],
        maximum_age: float,
    ) -> bool:
        if self._last_snapshot_scope is None or self._last_snapshot_mono <= 0.0:
            return False
        if time.monotonic() - self._last_snapshot_mono > maximum_age:
            return False
        return self._scope_satisfies(self._last_snapshot_scope, requested_scope)

    @staticmethod
    def _merge_account_refresh_scope(
        pending: tuple[str | None, bool] | None,
        requested: tuple[str | None, bool],
    ) -> tuple[str | None, bool]:
        if pending is None:
            return requested
        pending_symbol, pending_all_orders = pending
        symbol, all_open_orders = requested
        merged_all_orders = bool(pending_all_orders or all_open_orders)
        if merged_all_orders:
            return (None, True)
        if pending_symbol and symbol and pending_symbol != symbol:
            return (None, True)
        return (symbol or pending_symbol, False)

    def refresh_account(
        self,
        symbol: str | None = None,
        all_open_orders: bool = False,
        *,
        poll_minimum_interval: float = 0.0,
        follow_up: bool = False,
    ) -> None:
        if not self.has_credentials():
            self.problem.emit("API credentials are required to load account data.")
            return
        self.connect_session()
        symbol = str(symbol).upper().strip() if symbol else None
        requested_scope = (symbol, bool(all_open_orders))

        if poll_minimum_interval > 0.0 and self._snapshot_scope_is_fresh(
            requested_scope, float(poll_minimum_interval)
        ):
            return

        if self.account_task is not None:
            running_scope = self.account_task_scope
            running_satisfies = (
                running_scope is not None
                and self._scope_satisfies(running_scope, requested_scope)
            )
            if running_satisfies and not follow_up:
                return
            merged = self._merge_account_refresh_scope(
                self.pending_account_refresh, requested_scope
            )
            if merged != self.pending_account_refresh:
                self.pending_account_refresh = merged
            return

        if self.pending_account_refresh is not None:
            requested_scope = self._merge_account_refresh_scope(self.pending_account_refresh, requested_scope)
            self.pending_account_refresh = None
            symbol, all_open_orders = requested_scope
        task: ApiTask

        def done(payload: dict[str, Any]) -> None:
            self.tasks.discard(task)
            completed_scope = self.account_task_scope
            self.account_task = None
            self.account_task_scope = None
            events, self._refresh_events = self._refresh_events or [], None
            if self._refresh_events_overflow:
                self._refresh_events_overflow = False
                self.pending_account_refresh = self._merge_account_refresh_scope(self.pending_account_refresh, requested_scope)
                self.problem.emit("Account stream exceeded the reconciliation buffer; refreshing instead of publishing stale state.")
                self._run_pending_account_refresh()
                return
            payload = replay_account_events(payload, events)
            if completed_scope is not None:
                self._last_snapshot_scope = completed_scope
                self._last_snapshot_mono = refresh_started
            payload['_read_started_mono'] = refresh_started
            self._cache_snapshot(payload)
            self._last_account_snapshot = payload
            live_orders = {str(row.get('clientAlgoId') or row.get('clientOrderId') or ''): normalize_order(row)
                           for row in [*payload.get('orders', []), *payload.get('algoOrders', [])]}
            live_children = {(str(row.get('symbol') or ''), str(row.get('orderId') or '')): normalize_order(row)
                             for row in payload.get('orders', [])}
            scope = str(payload.get('ordersScope') or '')
            for client_id, record in self._protection_records.items():
                if (record.get('last_status') not in {'NEW', 'TRIGGERED', 'TRIGGERING', 'PARTIALLY_FILLED', 'PENDING_NEW'}
                        or scope not in {'ALL', str(record.get('order', {}).get('symbol') or '')}
                        or self._client_requests.get(client_id) in self.admitted_requests):
                    continue
                row = live_orders.get(client_id)
                if row is None:
                    row = live_children.get((str(record.get('order', {}).get('symbol') or ''),
                                             str(record.get('execution', {}).get('actualOrderId') or '')))
                if row is None or safe_float(row.get('executedQty')) > safe_float(record.get('execution', {}).get('executedQty')):
                    self._protection_recovery_pending = True
                    break
            self._retire_closed_protections(payload)
            for request_id, reservation in tuple(self._collateral_reservations.items()):
                accepted_at = reservation.get('accepted_at')
                if accepted_at is not None and accepted_at <= refresh_started:
                    self._collateral_reservations.pop(request_id, None)
            self.snapshot_ready.emit(payload)
            if self._protection_recovery_pending:
                self._recover_protections()
            self._run_pending_account_refresh()

        def failed(message: str) -> None:
            self.tasks.discard(task)
            self.account_task = None
            self.account_task_scope = None
            self._refresh_events = None
            self.problem.emit(f"Account refresh failed: {message}")
            self._run_pending_account_refresh()

        self._refresh_events = []
        self._refresh_events_overflow = False
        refresh_started = time.monotonic()
        task = self._launch_task(
            lambda: self.rest.account_snapshot(
                self.api_key,
                self.api_secret,
                symbol,
                all_open_orders,
            ),
            done,
            failed,
            self.task_pool,
        )
        self.tasks.add(task)
        self.account_task = task
        self.account_task_scope = requested_scope

    def _run_pending_account_refresh(self) -> None:
        if self.pending_account_refresh is None:
            return
        QTimer.singleShot(0, self, self._drain_pending_account_refresh)

    def _drain_pending_account_refresh(self) -> None:
        if self.stopping or self.account_task is not None:
            return
        pending, self.pending_account_refresh = self.pending_account_refresh, None
        if pending is not None:
            self.refresh_account(pending[0], pending[1], follow_up=False)

    def cancel_all(self, symbol: str) -> str:
        request_id = self._next_id("cancel-all")
        symbol = str(symbol).upper().strip()
        details = {
            "method": "orders.cancelAll",
            "context": {},
            "params": {"symbol": symbol},
            "sent": 0.0,
        }
        if self.stopping or not self.has_credentials():
            self._remember_failure(
                request_id,
                "API credentials are required; trading session unavailable.",
                False,
                details,
            )
            return request_id
        if not re.fullmatch(r"[A-Z0-9]{5,24}", symbol):
            self._remember_failure(
                request_id,
                "Cancel-all symbol is missing or invalid.",
                False,
                details,
            )
            return request_id
        task: ApiTask

        def done(payload: dict[str, Any]) -> None:
            self.tasks.discard(task)
            self.admitted_requests.discard(request_id)
            standard = payload.get("standard") or {}
            algo = payload.get("algo") or {}
            standard_error = (
                str(standard.get("error") or "") if isinstance(standard, dict) else ""
            )
            algo_error = str(algo.get("error") or "") if isinstance(algo, dict) else ""
            errors = [
                f"standard: {standard_error}" if standard_error else "",
                f"Algo: {algo_error}" if algo_error else "",
            ]
            errors = [error for error in errors if error]
            uncertain = bool(
                isinstance(standard, dict) and standard.get("uncertain")
                or isinstance(algo, dict) and algo.get("uncertain")
            )
            if errors and uncertain:
                self._finish_uncertain_nonplacement(
                    request_id,
                    details,
                    "Cancel-all completed only partially or with an unknown outcome · "
                    + " · ".join(errors),
                )
            elif errors:
                self._remember_failure(
                    request_id,
                    "Cancel-all completed only partially; Binance explicitly rejected: "
                    + " · ".join(errors),
                    False,
                    details,
                )
            else:
                self._finish_request(request_id, {**payload, "_transport": "REST"})
            self._queue_account_refresh(symbol)

        def failed(message: str) -> None:
            self.tasks.discard(task)
            self.admitted_requests.discard(request_id)
            self._handle_transport_failure(request_id, details, message)
            self._queue_account_refresh(symbol)

        self.admitted_requests.add(request_id)
        details["sent"] = time.monotonic()
        self._set_request_details(request_id, details)
        task = self._launch_task(
            lambda: self.rest.cancel_all_orders(self.api_key, self.api_secret, symbol),
            done,
            failed,
            self.task_pool,
        )
        self.tasks.add(task)
        return request_id

    def apply_cross_leverage(self, symbol: str, leverage: int) -> None:
        symbol = str(symbol).upper().strip()
        leverage = int(leverage)
        if not self.has_credentials():
            self.problem.emit("Add Binance API credentials before changing leverage.")
            return
        if not symbol or not (1 <= leverage <= 125):
            self.problem.emit("Choose a valid symbol and leverage between 1× and 125×.")
            return
        state_prefix = "ARMED" if self.armed else "SETTINGS"
        if symbol in self.cross_pending:
            self.queued_leverage[symbol] = leverage
            self.leverage_changing.emit(symbol, leverage)
            self.state_changed.emit(
                f"{state_prefix} · {symbol} · {leverage}× QUEUED AFTER CROSS CHECK"
            )
            return
        self.leverage_changing.emit(symbol, leverage)
        self.cross_pending.add(symbol)
        task: ApiTask

        cross_is_ready = symbol in self.cross_ready

        def execute() -> dict[str, Any]:
            if cross_is_ready:
                cross = {"symbol": symbol, "marginType": "CROSSED", "cached": True}
            else:
                try:
                    cross = self.rest.ensure_cross_margin(
                        self.api_key, self.api_secret, symbol
                    )
                except (RuntimeError, TimeoutError, OSError) as exc:
                    raise RuntimeError(f"CROSS MARGIN: {exc}") from exc
            try:
                changed = self.rest.change_leverage(
                    self.api_key, self.api_secret, symbol, leverage
                )
            except (RuntimeError, TimeoutError, OSError) as exc:
                raise RuntimeError(f"LEVERAGE: {exc}") from exc
            return {"cross": cross, "leverage": changed}

        def done(payload: dict[str, Any]) -> None:
            self.tasks.discard(task)
            self.cross_pending.discard(symbol)
            self.cross_ready.add(symbol)
            leverage_payload = payload.get("leverage") or {}
            actual_leverage = int(safe_float(leverage_payload.get("leverage")))
            if actual_leverage <= 0:
                actual_leverage = leverage
            for key, row in self.position_cache.items():
                if key[0] == symbol:
                    row["leverage"] = str(actual_leverage)
            self.leverage_cache[symbol] = actual_leverage
            prefix = "ARMED" if self.armed else "SETTINGS"
            self.state_changed.emit(
                f"{prefix} · {symbol} CROSS READY · {actual_leverage}×"
            )
            self.request_succeeded.emit(self._next_id("settings"), payload)
            self.leverage_changed.emit(symbol, actual_leverage, True, "")
            queued = self.queued_leverage.pop(symbol, 0)
            if queued and queued != actual_leverage:
                QTimer.singleShot(
                    0, lambda target=queued: self.apply_cross_leverage(symbol, target)
                )

        def failed(message: str) -> None:
            self.tasks.discard(task)
            self.cross_pending.discard(symbol)
            self.queued_leverage.pop(symbol, None)
            detail = self._settings_error(message)
            uncertain = execution_outcome_uncertain(message)
            if uncertain:
                detail += " The setting outcome is unknown; account state is being refreshed."
                self._queue_account_refresh(symbol)
            self.request_failed.emit(self._next_id("settings"), detail, uncertain)
            self.leverage_changed.emit(symbol, leverage, False, detail)

        task = self._launch_task(execute, done, failed, self.task_pool)
        self.tasks.add(task)

    def ensure_cross(self, symbol: str) -> None:
        if (
            self.stopping or not self.has_credentials()
            or not symbol
            or symbol in self.cross_ready
            or symbol in self.cross_pending
        ):
            return
        self.cross_pending.add(symbol)
        self.state_changed.emit(f"ARMED · VERIFYING {symbol} CROSS MARGIN")
        task: ApiTask

        def done(_payload: dict[str, Any]) -> None:
            self.tasks.discard(task)
            self.cross_pending.discard(symbol)
            self.cross_ready.add(symbol)
            self.state_changed.emit(f"ARMED · {symbol} CROSS READY")
            queued = self.queued_leverage.pop(symbol, 0)
            if queued:
                QTimer.singleShot(
                    0, lambda target=queued: self.apply_cross_leverage(symbol, target)
                )

        def failed(message: str) -> None:
            self.tasks.discard(task)
            self.cross_pending.discard(symbol)
            queued = self.queued_leverage.pop(symbol, 0)
            detail = self._settings_error(message)
            if execution_outcome_uncertain(message):
                detail += " The margin-mode outcome is unknown; account state is being refreshed."
                self._queue_account_refresh(symbol)
            if queued:
                self.leverage_changed.emit(symbol, queued, False, detail)
            self.problem.emit(f"Could not enable cross margin for {symbol}: {detail}")

        task = self._launch_task(
            lambda: self.rest.ensure_cross_margin(
                self.api_key, self.api_secret, symbol
            ),
            done,
            failed,
            self.task_pool,
        )
        self.tasks.add(task)

    def _user_connected(self) -> None:
        if self.stopping or not self.has_credentials():
            self.user_socket.close()
            return
        self.state_changed.emit("ACCOUNT STREAM LIVE")
        self.keepalive_timer.start()
        self.protection_poll_timer.stop()
        if self._protection_records:
            self._protection_recovery_pending = True


        self._queue_account_refresh()

    def _open_user_socket(self, listen_key: str) -> None:
        if not listen_key:
            return
        generation = self.stream_generation
        base = TEST_PRIVATE_WS if self.testnet else MAIN_PRIVATE_WS

        def open_when_allowed() -> None:
            if (
                self.stopping
                or not self.has_credentials()
                or generation != self.stream_generation
                or self.listen_key != listen_key
                or self.user_socket.state()
                in (
                    QtNetwork.QAbstractSocket.SocketState.ConnectedState,
                    QtNetwork.QAbstractSocket.SocketState.ConnectingState,
                )
            ):
                return
            delay = BINANCE_RATE_LIMITER.reserve_websocket_connection(api=False)
            if delay > 0:
                QTimer.singleShot(
                    min(60_000, max(250, round(delay * 1_000))),
                    open_when_allowed,
                )
                return
            self.user_socket.open(QUrl(f"{base}{listen_key}"))

        QTimer.singleShot(0, open_when_allowed)

    def _resume_user_stream(self) -> None:
        if self.stopping or not self.has_credentials():
            return
        if self.listen_key:
            self._open_user_socket(self.listen_key)
        else:
            self._start_user_stream()

    def _start_user_stream(self) -> None:
        if self.stopping or not self.has_credentials():
            return
        if self.user_socket.state() in (
            QtNetwork.QAbstractSocket.SocketState.ConnectedState,
            QtNetwork.QAbstractSocket.SocketState.ConnectingState,
        ):
            return
        if self.listen_key:
            self._open_user_socket(self.listen_key)
            return
        if self.user_stream_task is not None:
            return
        generation = self.stream_generation
        api_key = self.api_key
        task: ApiTask

        def done(listen_key: str) -> None:
            self.tasks.discard(task)
            if self.user_stream_task is task:
                self.user_stream_task = None
            if generation != self.stream_generation:
                if not self.stopping and self.has_credentials():
                    self.user_reconnect.start()
                return
            if not listen_key or self.stopping or not self.has_credentials():
                if not listen_key and not self.stopping and self.has_credentials():
                    self.problem.emit("Account stream returned an empty listen key.")
                    self.user_reconnect.start()
                return
            self.listen_key = listen_key
            self._open_user_socket(listen_key)
            self.keepalive_timer.start()

        def failed(message: str) -> None:
            self.tasks.discard(task)
            if self.user_stream_task is task:
                self.user_stream_task = None
            if generation != self.stream_generation:
                if not self.stopping and self.has_credentials():
                    self.user_reconnect.start()
                return
            self.problem.emit(f"Account stream unavailable: {message}")
            if not self.stopping and self.has_credentials():
                self.user_reconnect.start()

        task = self._launch_task(
            lambda: self.rest.start_user_stream(api_key),
            done,
            failed,
            self.task_pool,
        )
        self.user_stream_task = task
        self.tasks.add(task)

    def _replace_user_stream(self) -> None:
        self.stream_generation += 1
        self.keepalive_timer.stop()
        self.listen_key = ""
        self.user_socket.close()
        if not self.stopping and self.has_credentials():
            QTimer.singleShot(0, self._start_user_stream)

    def _keepalive_user_stream(self) -> None:
        if (
            not self.listen_key
            or not self.has_credentials()
            or self.keepalive_task is not None
        ):
            return
        generation = self.stream_generation
        listen_key = self.listen_key
        api_key = self.api_key
        task: ApiTask

        def done(payload: Any) -> None:
            self.tasks.discard(task)
            if self.keepalive_task is task:
                self.keepalive_task = None
            if generation != self.stream_generation:
                return
            returned_key = str(payload.get('listenKey') or '') if isinstance(payload, dict) else ''
            if not returned_key:
                self.problem.emit('Unexpected response: account-stream keepalive omitted its listen key; replacing the stream.')
                self._replace_user_stream()
            elif returned_key != self.listen_key:
                self.stream_generation += 1
                self.listen_key = returned_key
                self.user_socket.close()
                self._open_user_socket(returned_key)

        def failed(message: str) -> None:
            self.tasks.discard(task)
            if self.keepalive_task is task:
                self.keepalive_task = None
            if generation != self.stream_generation or self.stopping or not self.has_credentials():
                return
            self.problem.emit(f"Account-stream keepalive failed: {message}")
            if binance_error_code(message) == -1125 or "listen key" in message.casefold():
                self._replace_user_stream()
            else:
                QTimer.singleShot(30_000, self._keepalive_user_stream)

        task = self._launch_task(
            lambda: self.rest.keepalive_user_stream(api_key, listen_key),
            done,
            failed,
            self.task_pool,
        )
        self.keepalive_task = task
        self.tasks.add(task)

    def _user_disconnected(self) -> None:
        if not self.stopping and self.has_credentials():
            if self._protection_records:
                self._protection_recovery_pending = True
                self.protection_poll_timer.start()
                self._queue_account_refresh()
            self.state_changed.emit("ACCOUNT STREAM RECONNECTING")
            if not self.user_reconnect.isActive():
                self.user_reconnect.start()

    def _poll_disconnected_protections(self) -> None:
        if self.stopping or not self._protection_records or self.user_socket.state() == QtNetwork.QAbstractSocket.SocketState.ConnectedState:
            self.protection_poll_timer.stop()
            return
        self._recover_protections()

    def _user_error(self, _error: object) -> None:
        self.state_changed.emit("ACCOUNT STREAM OFFLINE")
        if (
            not self.stopping
            and self.has_credentials()
            and self.user_socket.state()
            == QtNetwork.QAbstractSocket.SocketState.UnconnectedState
            and not self.user_reconnect.isActive()
        ):
            self.user_reconnect.start()

    def _user_message(self, message: str) -> None:
        if self.stopping:
            return
        try:
            wrapper = json.loads(message)
        except json.JSONDecodeError:
            return


        payload = wrapper.get("data", wrapper) if isinstance(wrapper, dict) else {}
        if not isinstance(payload, dict):
            return
        if payload.get("e") == "listenKeyExpired":
            self._replace_user_stream()
            return
        event_type = str(payload.get("e", ""))
        order = normalize_order(payload.get('o') or payload.get('algoOrder') or {})
        if order and event_type in {'ORDER_TRADE_UPDATE', 'ALGO_UPDATE'}:
            order.setdefault('updateTime', event_timestamp(payload, order))
        if event_type in {'ORDER_TRADE_UPDATE', 'ALGO_UPDATE'} and not valid_order_fills(order):
            self.problem.emit('Invalid order fill data in the account stream; reconciling saved protections.')
            if self._protection_records:
                self._protection_recovery_pending = True
                self._recover_protections()
            return
        client_id = str(order.get('clientAlgoId') or order.get('clientOrderId') or '')
        if client_id and event_type in {'ORDER_TRADE_UPDATE', 'ALGO_UPDATE'}:
            request_id = self._client_requests.get(client_id)
            details = self.request_details.get(request_id) if request_id else None
            if details is not None and str(details.get('client_id', '')) == client_id:
                status = str(order.get('status') or 'NEW').upper()
                self.pending.pop(request_id, None)
                if not self.pending:
                    self.pending_timeout_timer.stop()
                self.admitted_requests.discard(request_id)
                cumulative_fill = safe_float(order.get('executedQty'))
                if status in {"REJECTED", "EXPIRED"} and cumulative_fill <= 0:
                    self._observe_protection_order(client_id, order)
                    reason = str(order.get("rm") or order.get("rejectReason") or "")
                    self._remember_failure(
                        request_id,
                        f"Binance account stream reported {status} for {client_id}"
                        + (f": {reason}" if reason else "."),
                        False,
                        details,
                    )
                else:
                    self._finish_request(request_id, {**order, '_transport': 'ACCOUNT STREAM'})
        if event_type == "ACCOUNT_CONFIG_UPDATE":
            self.rest.invalidate_position_mode_cache()
            configuration = payload.get("ac") or {}
            symbol = str(configuration.get("s") or "").upper()
            leverage = int(safe_float(configuration.get("l")))
            if symbol and leverage > 0:
                self.leverage_cache[symbol] = leverage
                for (cached_symbol, _side), row in self.position_cache.items():
                    if cached_symbol == symbol:
                        row["leverage"] = leverage
            mode = exchange_bool((payload.get("ai") or {}).get("j"))
            if mode is not None:
                self.multi_assets_margin = mode
        elif event_type == "MARGIN_CALL":
            positions = list(payload.get("p") or [])
            symbols = ", ".join(
                str(row.get("s")) for row in positions if row.get("s")
            )
            self.problem.emit(
                "BINANCE MARGIN CALL"
                + (f" · {symbols}" if symbols else " · inspect account risk immediately")
            )
        elif event_type == "CONDITIONAL_ORDER_TRIGGER_REJECT":
            rejection = payload.get("or") or payload.get("o") or payload
            symbol = str(rejection.get("s") or rejection.get("symbol") or "")
            reason = str(
                rejection.get("r")
                or rejection.get("rm")
                or rejection.get("reason")
                or rejection.get("rejectReason")
                or "trigger rejected"
            )
            self.problem.emit(
                "CONDITIONAL ORDER TRIGGER REJECTED"
                + (f" · {symbol}" if symbol else "")
                + f" · {reason}"
            )
        self._cache_account_event(payload)
        self.account_event.emit(payload)
        if event_type == 'ALGO_UPDATE' and order.get('status') == 'FINISHED' and self._protection_records:
            self._recover_protections()
        if event_type == 'CONDITIONAL_ORDER_TRIGGER_REJECT' and self._protection_records:
            self._protection_recovery_pending = True
            self._recover_protections()
        if event_type in {
            "ACCOUNT_UPDATE",
            "ORDER_TRADE_UPDATE",
            "ALGO_UPDATE",
            "ACCOUNT_CONFIG_UPDATE",
            "MARGIN_CALL",
            "CONDITIONAL_ORDER_TRIGGER_REJECT",
        }:
            symbol = str(order.get("s") or order.get("symbol") or "").upper()
            if not symbol and event_type == "ACCOUNT_UPDATE":
                positions = list((payload.get("a") or {}).get("P") or [])
                symbol = str((positions[0] if positions else {}).get("s") or "").upper()
            self._queue_account_refresh(symbol)

    def _cache_snapshot(self, payload: dict[str, Any]) -> None:
        account = payload.get("account") or {}
        config = payload.get("accountConfig") or {}
        can_trade = config.get("canTrade", account.get("canTrade"))
        self.account_can_trade = (
            can_trade
            if isinstance(can_trade, bool)
            else str(can_trade).casefold() == "true"
            if can_trade is not None
            else None
        )
        position_mode = payload.get("positionMode") or {}
        if "dualSidePosition" in position_mode:
            value = position_mode.get("dualSidePosition")
            self.hedge_mode = (
                value if isinstance(value, bool) else str(value).casefold() == "true"
            )
        self.multi_assets_margin = exchange_bool(config.get("multiAssetsMargin", account.get("multiAssetsMargin")))
        self.balance_cache = {
            str(row.get("asset")): dict(row)
            for row in account.get("assets", [])
            if row.get("asset")
        }
        self.position_cache = {
            (str(row.get("symbol")), str(row.get("positionSide", "BOTH"))): dict(row)
            for row in account.get("positions", [])
            if row.get("symbol")
        }
        margin_modes: dict[str, set[str]] = {}
        for row in payload.get("symbolConfig", []):
            symbol = str(row.get("symbol") or "")
            leverage = int(safe_float(row.get("leverage")))
            if symbol and leverage > 0:
                self.leverage_cache[symbol] = leverage
            mode = str(row.get("marginType") or "").lower()
            if symbol and mode:
                margin_modes.setdefault(symbol, set()).add(mode)
        for row in self.position_cache.values():
            symbol = str(row.get("symbol") or "")
            leverage = int(safe_float(row.get("leverage")))
            if symbol and leverage > 0:
                self.leverage_cache[symbol] = leverage
            margin_mode = str(row.get("marginType") or "").lower()
            if symbol and margin_mode:
                margin_modes.setdefault(symbol, set()).add(margin_mode)
        for symbol, modes in margin_modes.items():
            if modes.issubset({"cross", "crossed"}):
                self.cross_ready.add(symbol)
            else:
                self.cross_ready.discard(symbol)
        self.account_loaded = True

    def _cache_account_event(self, payload: dict[str, Any]) -> None:
        if self._refresh_events is not None:
            if len(self._refresh_events) < 4096:
                self._refresh_events.append(payload)
            else:
                self._refresh_events_overflow = True
        if payload.get('e') in {'ORDER_TRADE_UPDATE', 'ALGO_UPDATE'}:
            order = normalize_order(payload.get('o') or payload.get('a') or {})
            if order:
                order.setdefault('updateTime', event_timestamp(payload, order))
            self._observe_protection_order(self.protection_client_id(order), order)
        if payload.get("e") != "ACCOUNT_UPDATE":
            return


        for balance in self.balance_cache.values():
            balance.pop("availableBalance", None)
        account = payload.get("a") or {}
        stamp = event_timestamp(payload)
        for row in account.get("B", []):
            asset = str(row.get("a", ""))
            if asset:
                if older_than_row(stamp, self.balance_cache.get(asset, {})):
                    continue
                self.balance_cache[asset] = {
                    **self.balance_cache.get(asset, {}),
                    "asset": asset,
                    "walletBalance": row.get("wb", "0"),
                    "crossWalletBalance": row.get("cw", "0"),
                    "updateTime": stamp or self.balance_cache.get(asset, {}).get('updateTime', 0),
                }
        for row in account.get("P", []):
            key = (str(row.get("s", "")), str(row.get("ps", "BOTH")))
            if older_than_row(stamp, self.position_cache.get(key, {})):
                continue
            self.position_cache[key] = {
                **self.position_cache.get(key, {}),
                "symbol": key[0],
                "positionSide": key[1],
                "positionAmt": row.get("pa", "0"),
                "entryPrice": row.get("ep", "0"),
                "unrealizedProfit": row.get("up", "0"),
                "marginType": row.get("mt", "cross"),
                "updateTime": stamp or self.position_cache.get(key, {}).get('updateTime', 0),
            }
            margin_mode = str(row.get("mt") or "").lower()
            if key[0] and margin_mode in {"cross", "crossed"}:
                self.cross_ready.add(key[0])
            elif key[0] and margin_mode:
                self.cross_ready.discard(key[0])

    def current_leverage(self, symbol: str) -> int:
        if symbol in self.leverage_cache:
            return self.leverage_cache[symbol]
        values = [
            int(safe_float(row.get("leverage")))
            for (cached_symbol, _side), row in self.position_cache.items()
            if cached_symbol == symbol and safe_float(row.get("leverage")) > 0
        ]
        return values[0] if values and len(set(values)) == 1 else 0

    def has_open_position(self, symbol: str, position_side: str | None = None) -> bool:
        return any(
            cached_symbol == symbol and (position_side is None or side == position_side)
            and abs(safe_float(row.get("positionAmt"))) > 0
            for (cached_symbol, side), row in self.position_cache.items()
        )

    def open_position_symbols(self) -> tuple[str, ...]:
        """Return every symbol with non-zero exposure in the account cache."""
        return tuple(
            sorted(
                {
                    cached_symbol
                    for (cached_symbol, _side), row in self.position_cache.items()
                    if cached_symbol and abs(safe_float(row.get("positionAmt"))) > 0
                }
            )
        )

    def available_balance(self, asset: str) -> float:
        if not self.account_loaded or self._last_snapshot_mono <= 0 or time.monotonic() - self._last_snapshot_mono > 30.0:
            raise ValueError('Account collateral is stale or still loading; refresh before sizing another entry.')
        if self.multi_assets_margin is not False:
            raise ValueError("Collateral shortcuts require confirmed Single-Asset Mode. Multi-Assets USD collateral needs an explicit conversion policy; use a quantity-based order.")
        row = self.balance_cache.get(asset) or {}
        if "availableBalance" in row:
            reserved = sum(item['amount'] for item in self._collateral_reservations.values() if item['asset'] == asset)
            return max(0.0, safe_float(row.get("availableBalance")) - reserved)
        return 0.0

    def stop(self) -> None:
        self.stopping = True
        self._lifecycle += 1
        self._client_requests.clear()
        self.trade_connected = False
        for token in self._protection_cancel_tokens.values():
            token.set()
        for task in tuple(self.tasks):
            task.cancel()


        for request_id, details in list(self.request_details.items()):
            fingerprint = str(details.get('fingerprint') or '')
            if details.get('method') in {'order.place', 'algoOrder.place', 'batchOrders.place'}:
                details['transport_state'] = 'uncertain'
                if fingerprint:
                    self.inflight_fingerprints.pop(fingerprint, None)
                    self.unresolved_fingerprints[fingerprint] = request_id
            else:
                self._pop_request_details(request_id, None)
                self._mark_terminal(request_id)
        self.pending.clear()
        self.admitted_requests.clear()
        self.reconciling.clear()
        self.account_task = None
        self.account_task_scope = None
        self.pending_account_refresh = None
        self.user_stream_task = None
        self.keepalive_task = None
        self.armed = False
        self.armed_changed.emit(False)
        self.trade_reconnect.stop()
        self.user_reconnect.stop()
        self.keepalive_timer.stop()
        self.pending_timeout_timer.stop()
        self.account_event_refresh_timer.stop()
        self.protection_poll_timer.stop()
        self.stream_generation += 1
        self.account_event_refresh_symbols.clear()
        self.queued_leverage.clear()
        self.trade_socket.close()
        self.user_socket.close()
