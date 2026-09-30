"""Binance execution adapter and account lifecycle."""
from __future__ import annotations




import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
import urllib.parse
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
                if operation == "load":
                    return connection.execute(
                        "SELECT request_id, payload FROM unresolved_placements WHERE scope=?", (self.scope,)
                    ).fetchall()
                if operation == "put":
                    connection.execute(
                        "INSERT INTO unresolved_placements VALUES (?, ?, ?, ?)",
                        (self.scope, request_id, fingerprint, payload),
                    )
                elif operation == "delete":
                    connection.execute(
                        "DELETE FROM unresolved_placements WHERE scope=? AND request_id=?", (self.scope, request_id)
                    )
        finally:
            connection.close()
            if self._diagnostics is not None:
                self._diagnostics.observe_ms("trading.journal_ms", (time.perf_counter() - started) * 1000.0)


class TradingGateway(QtCore.QObject):
    """One-session Binance trading transport with explicit uncertainty handling."""

    MAX_OPEN_ORDER_WRITES = 16

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
        self.account_task: ApiTask | None = None
        self.account_task_scope: tuple[str | None, bool] | None = None
        self.pending_account_refresh: tuple[str | None, bool] | None = None
        self._last_snapshot_mono = 0.0
        self._last_snapshot_scope: tuple[str | None, bool] | None = None
        self.balance_cache: dict[str, dict[str, Any]] = {}
        self.account_available_balance: float | None = None
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
        self._journal_pool = QtCore.QThreadPool(self)
        self._journal_pool.setMaxThreadCount(1)
        self._journal: _PlacementJournal | None = None
        self._journal_loading = True
        self._journal_error = ""
        self._journal_requests: set[str] = set()
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

        def restored(rows) -> None:
            try:
                restored_details = []
                for request_id, payload in rows:
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
            for request_id, details in restored_details:
                self._set_request_details(request_id, details)
                self._journal_requests.add(request_id)
                self.unresolved_fingerprints[details["fingerprint"]] = request_id
            self._journal_loading = False
            if restored_details:
                self.state_changed.emit(f"RESTORED {len(restored_details)} UNRESOLVED ORDER INTENTS")
                self.reconcile_unknown_orders()

        task = self._launch_task(lambda: journal.run("load"), restored, self._journal_failed, self._journal_pool)
        self.tasks.add(task)

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
            serialized = json.dumps(unsigned(saved), separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            self._remember_failure(request_id, f"Order intent could not be saved: {exc}", False, details)
            return
        self._set_request_details(request_id, details)
        self.inflight_fingerprints[str(details["fingerprint"])] = request_id
        self.admitted_requests.add(request_id)
        self._journal_requests.add(request_id)

        def committed(_result) -> None:
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
        ):
            self.problem.emit(
                "Credentials cannot be replaced while a Binance request is in flight or unresolved. "
                "Reconcile the request or restart Nightwatch first."
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
        self.trade_socket.close()
        self.user_socket.close()
        self.trade_connected = False
        self._bind_session_sockets()
        self.listen_key = ""
        self.balance_cache.clear()
        self.account_available_balance = None
        self.account_can_trade = None
        self.position_cache.clear()
        self.leverage_cache.clear()
        self.hedge_mode = None
        self.account_loaded = False
        self.pending_account_refresh = None
        self._last_snapshot_mono = 0.0
        self._last_snapshot_scope = None
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
        if self.account_can_trade is False:
            self.problem.emit(
                "Binance reports that Futures trading is disabled for this account or API key."
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

    def _open_order_write_count(self) -> int:
        return sum(
            len(details.get("expected_orders", []))
            if details.get("method") == "batchOrders.place"
            else 1
            for request_id, details in self.request_details.items()
            if request_id not in self.terminal_requests
            and details.get("method")
            in {"order.place", "algoOrder.place", "batchOrders.place"}
        )

    def placement_status(self) -> tuple[int, int, int]:
        """Used slots, limit and unresolved requests (including restored intents)."""
        unknown = len(set(self.unresolved_fingerprints.values()) - self.terminal_requests)
        return self._open_order_write_count(), self.MAX_OPEN_ORDER_WRITES, unknown

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
        reduce_only = payload.get("reduceOnly") is True or str(
            payload.get("reduceOnly", "")
        ).casefold() == "true"
        close_position = payload.get("closePosition") is True or str(
            payload.get("closePosition", "")
        ).casefold() == "true"
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
        if close_position:
            if order_type not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}:
                raise ValueError(
                    "closePosition is valid only for Stop Market and Take Profit Market orders."
                )
            if payload.get("quantity") not in (None, ""):
                raise ValueError("A close-all conditional order cannot include quantity.")
            if reduce_only:
                raise ValueError("closePosition and reduceOnly cannot be sent together.")
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
                str(payload.get("quantity", "")), step, "Quantity"
            )
            quantity = safe_float(payload["quantity"])
            minimum_qty = rules.min_market_qty if market_quantity else rules.min_qty
            maximum_qty = rules.max_market_qty if market_quantity else rules.max_qty
            if not (minimum_qty <= quantity <= maximum_qty):
                raise ValueError(
                    f"Quantity must be between {minimum_qty:g} and {maximum_qty:g}."
                )
            for key, label in (
                ("price", "Order price"),
                ("triggerPrice", "Trigger price"),
                ("activatePrice", "Activation price"),
            ):
                if payload.get(key) not in (None, ""):
                    payload[key] = validate_step(
                        str(payload[key]), rules.tick_size, label
                    )
        elif not close_position and safe_float(payload.get("quantity")) <= 0:
            raise ValueError("Order quantity must be positive.")
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
            time_in_force = str(payload.get("timeInForce", "")).upper()
            if time_in_force not in {"GTC", "IOC", "FOK", "GTX", "GTD", "RPI"}:
                raise ValueError("A valid time-in-force is required for this order type.")
            payload["timeInForce"] = time_in_force
            if time_in_force == "GTD":
                good_till = int(safe_float(payload.get("goodTillDate")))
                now_ms = int(time.time() * 1_000)
                if not (now_ms + 600_000 < good_till < 253_402_300_799_000):
                    raise ValueError(
                        "GTD goodTillDate must be more than 600 seconds ahead and within Binance's supported range."
                    )
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
        client_id = str(payload.get("newClientOrderId") or payload.get("clientAlgoId") or "")
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
        context = details.get("context")
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
        method = str(details.get("method") or "request")
        self.request_failed.emit(request_id, message, uncertain)

    def _finish_request(self, request_id: str, result: Any) -> None:
        if request_id in self.terminal_requests:
            return
        details = self._pop_request_details(request_id, {})
        fingerprint = str(details.get("fingerprint", ""))
        if fingerprint:
            self.inflight_fingerprints.pop(fingerprint, None)
            self.unresolved_fingerprints.pop(fingerprint, None)
        self.reconciling.discard(request_id)
        expected = details.get("expected") or {}
        if isinstance(result, dict):
            expected_client = str(details.get("client_id", ""))
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
        self.request_succeeded.emit(request_id, result)

    @staticmethod
    def _response_mismatches(expected: dict[str, Any], result: dict[str, Any], client_id: str = "") -> list[str]:
        mismatches = []
        fields = {
            "symbol": ("symbol",), "side": ("side",), "type": ("type", "orderType"),
            "quantity": ("origQty", "quantity"), "price": ("price",),
            "triggerPrice": ("triggerPrice", "stopPrice"), "activatePrice": ("activatePrice", "activationPrice"),
            "callbackRate": ("callbackRate", "priceRate"), "positionSide": ("positionSide",),
            "reduceOnly": ("reduceOnly",), "closePosition": ("closePosition",), "priceProtect": ("priceProtect",),
            "workingType": ("workingType",), "timeInForce": ("timeInForce",),
            "priceMatch": ("priceMatch",), "selfTradePreventionMode": ("selfTradePreventionMode",),
            "goodTillDate": ("goodTillDate",),
        }
        numeric = {"quantity", "price", "triggerPrice", "activatePrice", "callbackRate", "goodTillDate"}
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
            if result is None or self._response_mismatches(expected, result, client_id):
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
        message = self.trade_socket.errorString().strip()
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
        if not self.stopping and self.has_credentials():
            pass
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
        elif uncertain:
            self._finish_uncertain_nonplacement(request_id, metadata, message)
        else:
            self._remember_failure(request_id, message, False, metadata)

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
        query = urllib.parse.urlencode(sorted(encoded.items()))
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

        def query() -> dict[str, Any]:
            last_error = reason
            for attempt in range(6):
                if attempt:
                    time.sleep(0.8)
                try:
                    return self.rest.query_order_by_client_id(
                        self.api_key,
                        self.api_secret,
                        symbol,
                        client_id,
                        algo=method == "algoOrder.place",
                    )
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
        candidates = [
            (request_id, dict(details))
            for request_id, details in self.request_details.items()
            if str(details.get("fingerprint", "")) in self.unresolved_fingerprints
        ]
        if not candidates:
            self.problem.emit("No unresolved order placements are waiting for reconciliation.")
            return
        for request_id, details in candidates:
            if str(details.get("method", "")) == "batchOrders.place":
                self._begin_batch_reconciliation(
                    request_id,
                    details,
                    "Manual batch reconciliation requested.",
                )
            else:
                self._begin_reconciliation(
                    request_id,
                    details,
                    "Manual reconciliation requested.",
                )

    def submit_order(self, order: dict[str, Any], context: dict[str, Any] | None = None) -> str:
        request_id = self._next_id("order")
        context = context or {}
        emergency = bool(context.get("emergency_close"))
        if self.stopping or not self.has_credentials():
            self._remember_failure(request_id, "API credentials are required; trading session unavailable.", False,
                                   {"context": context})
            return request_id
        if context.get("requires_arm") and not self.armed and not emergency:
            self._remember_failure(
                request_id,
                "Trading is disarmed.",
                False,
                {"context": context},
            )
            return request_id
        if not emergency and self._open_order_write_count() >= self.MAX_OPEN_ORDER_WRITES:
            self._remember_failure(
                request_id,
                "Too many order placements are still awaiting a final Binance outcome. "
                "Wait or reconcile them before sending more.",
                False,
                {"context": context},
            )
            return request_id
        symbol = str(order.get("symbol", ""))
        if symbol and symbol in self.cross_pending and not emergency:
            self._remember_failure(
                request_id,
                "Margin mode or leverage is still being applied for this symbol. Wait for CROSS READY, then submit again.",
                False,
                {"context": context},
            )
            return request_id
        if symbol and symbol not in self.cross_ready and not emergency and not context.get("existing_order_replacement"):
            self.ensure_cross(symbol)
            self._remember_failure(
                request_id,
                "Cross margin is still being verified for this symbol. Wait for CROSS READY, then submit again.",
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
        fingerprint = self._order_fingerprint(payload)
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
        details = {
            "method": method,
            "context": context,
            "expected": dict(payload),
            "client_id": client_id,
            "fingerprint": fingerprint,
            "sent": 0.0,
        }
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
            if isinstance(rules, SymbolRules):
                payload["quantity"] = validate_step(
                    str(payload.get("quantity", "")), rules.lot_step, "Quantity"
                )
                payload["price"] = validate_step(
                    str(payload.get("price", "")), rules.tick_size, "Price"
                )
                quantity = safe_float(payload["quantity"])
                price = safe_float(payload["price"])
                if not (rules.min_qty <= quantity <= rules.max_qty):
                    raise ValueError(
                        f"Quantity must be between {rules.min_qty:g} and {rules.max_qty:g}."
                    )
                if not (rules.min_price <= price <= rules.max_price):
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
        }
        if self.stopping or not self.has_credentials():
            self._remember_failure(request_id, "API credentials are required; trading session unavailable.", False,
                                   {"context": context})
            return request_id
        if requires_arm and not self.armed:
            self._remember_failure(
                request_id, "Trading is disarmed.", False, {"context": context}
            )
            return request_id
        symbol = str((orders[0] if orders else {}).get("symbol", ""))
        if symbol and symbol in self.cross_pending:
            self._remember_failure(
                request_id,
                "Margin mode or leverage is still being applied. Wait for CROSS READY, then submit the batch again.",
                False,
                {"context": context},
            )
            return request_id
        if symbol and symbol not in self.cross_ready:
            self.ensure_cross(symbol)
            self._remember_failure(
                request_id,
                "Cross margin is still being verified. Wait for CROSS READY, then submit the batch again.",
                False,
                {"context": context},
            )
            return request_id
        if not orders:
            self._remember_failure(
                request_id, "The batch contains no orders.", False, {"context": context}
            )
            return request_id
        if len(orders) > 5:
            self._remember_failure(
                request_id,
                "Binance USD-M batches accept at most five orders; nothing was sent.",
                False,
                {"context": context},
            )
            return request_id
        if self._open_order_write_count() + len(orders) > self.MAX_OPEN_ORDER_WRITES:
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
                prepared.append(item)
        except ValueError as exc:
            self._remember_failure(
                request_id,
                f"Batch validation failed: {exc}",
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
            "client_ids": [str(order.get("newClientOrderId", "")) for order in prepared],
            "fingerprint": fingerprint,
            "sent": time.monotonic(),
        }
        task: ApiTask

        def done(result: Any) -> None:
            self.tasks.discard(task)
            self.admitted_requests.discard(request_id)
            if request_id in self.terminal_requests:
                return
            rows = list(result) if isinstance(result, list) else []
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
                accepted = len(rows) - len(errors)
                transport_uncertain = any(
                    bool(row.get("_uncertain"))
                    for row in errors
                    if isinstance(row, dict)
                )
                first = errors[0]
                code = int(safe_float(first.get("code")))
                detail = str(first.get("msg") or first.get("message") or "rejected")
                self._remember_failure(
                    request_id,
                    f"Batch completed with {accepted} accepted and {len(errors)} rejected order(s). "
                    f"First rejection: {code} · {detail}. "
                    + (
                        "Do not resubmit the complete batch; review the accepted client IDs first."
                        if accepted
                        else "No order in the batch was accepted."
                    ),
                    accepted > 0 or transport_uncertain,
                    details,
                )
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
            task = self._launch_task(
                lambda: self.rest.place_batch_orders(self.api_key, self.api_secret, prepared),
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
                        found = self.rest.query_order_by_client_id(
                            self.api_key,
                            self.api_secret,
                            symbol,
                            client_id,
                            algo=algo,
                        )
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

    def submit_cancel(self, request: dict[str, Any], algo: bool = False) -> str:
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
            {},
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
        if (self.trade_connected and self.rest.has_fresh_time_offset()
                and self.trade_socket.state() == QtNetwork.QAbstractSocket.SocketState.ConnectedState):

            try:
                signed = {key: value for key, value in params.items() if value is not None and value != ''}
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

        def execute() -> Any:
            if method in {"order.place", "algoOrder.place"}:
                return self.rest.place_order(self.api_key, self.api_secret, params)
            if method == "order.modify":
                return self.rest.modify_order(self.api_key, self.api_secret, params)
            return self.rest.cancel_order(
                self.api_key,
                self.api_secret,
                params,
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
            if 'rate window' in message.lower() or 'published rate limit' in message.lower():
                metadata['transport_state'] = 'rate_limited'
                self._remember_failure(request_id, 'Local rate limit; not sent: ' + message, False, metadata)
            else:
                self._handle_transport_failure(request_id, metadata, message)

        task = self._launch_task(execute, done, failed, self.task_pool)
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
            else:
                pass
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
            if completed_scope is not None:
                self._last_snapshot_scope = completed_scope
                self._last_snapshot_mono = time.monotonic()
            self._cache_snapshot(payload)
            self.snapshot_ready.emit(payload)
            self._run_pending_account_refresh()

        def failed(message: str) -> None:
            self.tasks.discard(task)
            self.account_task = None
            self.account_task_scope = None
            self.problem.emit(f"Account refresh failed: {message}")
            self._run_pending_account_refresh()

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

        def done(_payload: Any) -> None:
            self.tasks.discard(task)
            if self.keepalive_task is task:
                self.keepalive_task = None

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
            self.state_changed.emit("ACCOUNT STREAM RECONNECTING")
            if not self.user_reconnect.isActive():
                self.user_reconnect.start()

    def _user_error(self, _error: object) -> None:
        message = self.user_socket.errorString().strip()
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
        order = payload.get("o") or payload.get("a") or payload.get("algoOrder") or {}
        client_id = str(
            order.get("c")
            or order.get("caid")
            or order.get("clientOrderId")
            or order.get("clientAlgoId")
            or ""
        )
        if client_id:
            request_id = self._client_requests.get(client_id)
            details = self.request_details.get(request_id) if request_id else None
            if details is not None and str(details.get('client_id', '')) == client_id:
                status = str(
                    order.get("X")
                    or order.get("orderStatus")
                    or order.get("status")
                    or "NEW"
                ).upper()
                self.pending.pop(request_id, None)
                if not self.pending:
                    self.pending_timeout_timer.stop()
                self.admitted_requests.discard(request_id)
                cumulative_fill = safe_float(order.get("z") or order.get("aq") or order.get("executedQty") or order.get("cumQty"))
                if status in {"REJECTED", "EXPIRED"} and cumulative_fill <= 0:
                    reason = str(order.get("rm") or order.get("rejectReason") or "")
                    self._remember_failure(
                        request_id,
                        f"Binance account stream reported {status} for {client_id}"
                        + (f": {reason}" if reason else "."),
                        False,
                        details,
                    )
                else:
                    normalized = {
                        "symbol": order.get("s") or order.get("symbol"),
                        "side": order.get("S") or order.get("side"),
                        "type": order.get("o") or order.get("type") or order.get("orderType"),
                        "clientOrderId": client_id if event_type == "ORDER_TRADE_UPDATE" else "",
                        "clientAlgoId": client_id if event_type != "ORDER_TRADE_UPDATE" else "",
                        "orderId": order.get("i") or order.get("orderId"),
                        "algoId": order.get("aid") or order.get("algoId"),
                        "status": status,
                        "executedQty": (
                            order.get("z")
                            or order.get("aq")
                            or order.get("executedQty")
                            or order.get("cumQty")
                        ),
                        "avgPrice": order.get("ap") or order.get("avgPrice"),
                        "_transport": "ACCOUNT STREAM",
                    }
                    for name, aliases in {
                        "origQty": ("q", "origQty", "quantity"), "price": ("p", "price"),
                        "stopPrice": ("sp", "triggerPrice", "stopPrice"), "positionSide": ("ps", "positionSide"),
                        "reduceOnly": ("R", "reduceOnly"), "closePosition": ("cp", "closePosition"),
                        "workingType": ("wt", "workingType"), "timeInForce": ("f", "timeInForce"),
                        "activatePrice": ("AP", "activatePrice", "activationPrice"),
                        "callbackRate": ("cr", "callbackRate", "priceRate"),
                    }.items():
                        source = next((key for key in aliases if key in order), None)
                        if source is not None:
                            normalized[name] = order[source]
                    self._finish_request(request_id, normalized)
        if event_type == "ACCOUNT_CONFIG_UPDATE":
            self.rest.invalidate_position_mode_cache()
            configuration = payload.get("ac") or {}
            symbol = str(configuration.get("s") or "").upper()
            leverage = int(safe_float(configuration.get("l")))
            if symbol and leverage > 0:
                self.leverage_cache[symbol] = leverage
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
        can_trade = account.get("canTrade")
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
        self.account_available_balance = (
            max(0.0, safe_float(account.get("availableBalance")))
            if "availableBalance" in account
            else None
        )
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
        if payload.get("e") != "ACCOUNT_UPDATE":
            return



        self.account_available_balance = None
        account = payload.get("a") or {}
        for row in account.get("B", []):
            asset = str(row.get("a", ""))
            if asset:
                self.balance_cache[asset] = {
                    "asset": asset,
                    "walletBalance": row.get("wb", "0"),
                    "crossWalletBalance": row.get("cw", "0"),
                }
        for row in account.get("P", []):
            key = (str(row.get("s", "")), str(row.get("ps", "BOTH")))
            self.position_cache[key] = {
                **self.position_cache.get(key, {}),
                "symbol": key[0],
                "positionSide": key[1],
                "positionAmt": row.get("pa", "0"),
                "entryPrice": row.get("ep", "0"),
                "unrealizedProfit": row.get("up", "0"),
                "marginType": row.get("mt", "cross"),
            }
            margin_mode = str(row.get("mt") or "").lower()
            if key[0] and margin_mode in {"cross", "crossed"}:
                self.cross_ready.add(key[0])
            elif key[0] and margin_mode:
                self.cross_ready.discard(key[0])

    def current_leverage(self, symbol: str) -> int:
        values = [
            int(safe_float(row.get("leverage")))
            for (cached_symbol, _side), row in self.position_cache.items()
            if cached_symbol == symbol and safe_float(row.get("leverage")) > 0
        ]
        return max([self.leverage_cache.get(symbol, 0), *values], default=0)

    def has_open_position(self, symbol: str) -> bool:
        return any(
            cached_symbol == symbol and abs(safe_float(row.get("positionAmt"))) > 0
            for (cached_symbol, _side), row in self.position_cache.items()
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




        if self.account_available_balance is not None:
            return self.account_available_balance
        row = self.balance_cache.get(asset) or {}
        if "availableBalance" in row:
            return max(0.0, safe_float(row.get("availableBalance")))
        return 0.0

    def stop(self) -> None:
        self.stopping = True
        self._lifecycle += 1
        self._client_requests.clear()
        self.trade_connected = False


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
        self.stream_generation += 1
        self.account_event_refresh_symbols.clear()
        self.queued_leverage.clear()
        self.trade_socket.close()
        self.user_socket.close()
