"""Binance-specific REST/archive access, throttling and error policy."""
from __future__ import annotations


import asyncio
import atexit
import contextvars
import concurrent.futures
import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Coroutine
from urllib.parse import urlsplit
import httpx
from ..constants import APP_NAME

log = logging.getLogger(__name__)


class RateLimitDeferred(BaseException):
    """An unsent read can resume later without occupying a Qt worker."""

    def __init__(self, delay: float):
        self.delay = max(0.05, float(delay))


DEFER_RATE_WAITS = contextvars.ContextVar('defer_rate_waits', default=False)
REQUEST_SOURCE = contextvars.ContextVar('request_source', default='')
READ_RESULTS = contextvars.ContextVar('read_results', default=None)
TASK_CANCELLED = contextvars.ContextVar('task_cancelled', default=None)
_SHUTDOWN_CALLBACKS: list[Callable[[], None]] = []


def register_network_shutdown(callback: Callable[[], None]) -> None:
    _SHUTDOWN_CALLBACKS.append(callback)

class _AsyncHttpRuntime:
    """One process-wide asyncio/httpx transport on one daemon thread."""

    def __init__(self) -> None:
        self._start_lock = threading.Lock()
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client: httpx.AsyncClient | None = None
        self._last_slow_log: dict[str, float] = {}
        self._active_requests = 0
        self._closed = False
        self._qt_hooked = False
        self._start_error: Exception | None = None

    def _ensure_started(self) -> None:
        if self._closed:
            raise RuntimeError('Network transport is shutting down.')
        app = QtCore.QCoreApplication.instance()
        if app is not None and not self._qt_hooked and QtCore.QThread.currentThread() == app.thread():
            app.aboutToQuit.connect(shutdown_networking)
            self._qt_hooked = True
        thread = self._thread
        if thread is not None and thread.is_alive() and self._ready.is_set():
            return
        with self._start_lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._ready.clear()
                self._start_error = None
                thread = threading.Thread(target=self._run_loop, name='nightwatch-async-http', daemon=True)
                self._thread = thread
                thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError('Network transport failed to start.')
        if self._start_error is not None:
            raise RuntimeError(f'Network transport failed to start: {self._start_error}') from self._start_error

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        limits = httpx.Limits(max_connections=100, max_keepalive_connections=32, keepalive_expiry=15.0)
        self._loop = loop
        try:
            self._client = httpx.AsyncClient(limits=limits, follow_redirects=True, http2=False, trust_env=True)
        except Exception as exc:
            self._start_error = exc
            self._ready.set()
            loop.close()
            return
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            client = self._client
            self._client = None
            if client is not None:
                try:
                    loop.run_until_complete(client.aclose())
                except Exception:
                    pass
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                try:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except Exception:
                    pass
            loop.close()

    def close(self) -> None:
        """Stop new work; drain admitted HTTP calls before closing the loop."""
        with self._start_lock:
            if self._closed:
                return
            self._closed = True
            loop, thread = self._loop, self._thread
        if loop is None or thread is None or not thread.is_alive():
            return

        async def drain() -> None:


            deadline = time.monotonic() + 15.0
            while self._active_requests and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            loop.stop()

        asyncio.run_coroutine_threadsafe(drain(), loop)

    def submit(self, coroutine: Coroutine[Any, Any, Any]) -> concurrent.futures.Future[Any]:
        try:
            self._ensure_started()
        except Exception:
            coroutine.close()
            raise
        loop = self._loop
        if loop is None or loop.is_closed():
            coroutine.close()
            raise RuntimeError('Network transport is unavailable.')
        return asyncio.run_coroutine_threadsafe(coroutine, loop)

    def run(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        if threading.current_thread() is self._thread:
            try:
                coroutine.close()
            except Exception:
                pass
            raise RuntimeError('Synchronous wait attempted on the async transport thread.')
        future = self.submit(coroutine)
        cancelled = TASK_CANCELLED.get()
        while True:
            if cancelled is not None and cancelled.is_set():
                future.cancel()
                raise concurrent.futures.CancelledError('Request cancelled.')
            try:
                return future.result(timeout=0.1)
            except concurrent.futures.TimeoutError:
                if future.done():
                    return future.result()

    async def request_json(self, url: str, params: dict[str, Any] | None=None, method: str='GET', headers: dict[str, str] | None=None, body: bytes | None=None, timeout: float=12.0, response_hook: Callable[[int, Any], None] | None=None) -> Any:
        client = self._client
        if self._closed:
            raise RuntimeError('Network transport is shutting down.')
        if client is None:
            raise RuntimeError('Network transport is unavailable.')
        method_name = method.upper()
        path = urlsplit(url).path or url
        started = time.perf_counter()
        self._active_requests += 1
        try:
            response = await client.request(method_name, url, params=params, headers={'User-Agent': f'{APP_NAME}/1.0', **(headers or {})}, content=body, timeout=timeout)
        except httpx.TimeoutException as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            raise RuntimeError('Network request timed out.') from exc
        except httpx.RequestError as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            message = str(exc).strip() or exc.__class__.__name__
            raise RuntimeError(f'Network error: {message}') from exc
        finally:
            self._active_requests -= 1
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if elapsed_ms >= 750.0:
            slow_key = f'{method_name} {path}'
            symbol = str((params or {}).get('symbol') or '')
            subject = f' · {symbol}' if symbol else ''
            now = time.monotonic()
            if elapsed_ms >= 2500.0 and now - self._last_slow_log.get(slow_key, 0.0) >= 10.0:
                self._last_slow_log[slow_key] = now
                log.warning('Slow REST request %s%s · %.0f ms', slow_key, subject, elapsed_ms)
            else:
                log.debug('REST request latency %s%s · %.0f ms', slow_key, subject, elapsed_ms)
        if response_hook is not None:
            response_hook(int(response.status_code), response.headers)
        try:
            raw = response.content.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise RuntimeError('Unexpected response: remote service returned non-UTF-8 data.') from exc
        if response.status_code >= 300:
            symbol = str((params or {}).get('symbol') or '')
            subject = f' · {symbol}' if symbol else ''
            try:
                payload = json.loads(raw) if raw else {}
                if isinstance(payload, dict):
                    message = payload.get('msg') or payload.get('message') or raw or response.reason_phrase
                    binance_code = payload.get('code')
                else:
                    message = raw or response.reason_phrase
                    binance_code = None
            except json.JSONDecodeError:
                message = raw or response.reason_phrase
                binance_code = None
            code_text = f' · Binance {binance_code}' if binance_code is not None else ''
            raise RuntimeError(f'HTTP {response.status_code}{code_text}: {message}')
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError('Unexpected response: remote service returned invalid JSON.') from exc

    async def request_bytes(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 12.0,
        max_bytes: int = 2_000_000,
    ) -> bytes:
        """Fetch a bounded binary asset through the process-wide HTTP transport."""
        client = self._client
        if self._closed:
            raise RuntimeError('Network transport is shutting down.')
        if client is None:
            raise RuntimeError('Network transport is unavailable.')
        path = urlsplit(url).path or url
        started = time.perf_counter()
        self._active_requests += 1
        try:
            ceiling = max(1, int(max_bytes))
            async with client.stream('GET', url, headers={'User-Agent': f'{APP_NAME}/1.0', **(headers or {})}, timeout=timeout) as response:
                if response.status_code >= 300:
                    raise RuntimeError(f'HTTP {response.status_code}: {response.reason_phrase}')
                declared = response.headers.get('content-length')
                if declared and declared.isdigit() and int(declared) > ceiling:
                    raise RuntimeError(f'Remote asset exceeds {ceiling:,} bytes.')
                payload = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=min(64 * 1024, ceiling + 1)):
                    if len(payload) + len(chunk) > ceiling:
                        raise RuntimeError(f'Remote asset exceeds {ceiling:,} bytes.')
                    payload.extend(chunk)
        except httpx.TimeoutException as exc:
            raise RuntimeError('Network request timed out.') from exc
        except httpx.RequestError as exc:
            message = str(exc).strip() or exc.__class__.__name__
            raise RuntimeError(f'Network error: {message}') from exc
        finally:
            self._active_requests -= 1
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if elapsed_ms >= 750.0:
            slow_key = f'GET {path}'
            now = time.monotonic()
            if elapsed_ms >= 2500.0 and now - self._last_slow_log.get(slow_key, 0.0) >= 10.0:
                self._last_slow_log[slow_key] = now
                log.warning('Slow asset request %s · %.0f ms', slow_key, elapsed_ms)
            else:
                log.debug('Asset request latency %s · %.0f ms', slow_key, elapsed_ms)
        return bytes(payload)

_ASYNC_HTTP = _AsyncHttpRuntime()


def shutdown_networking() -> None:
    for callback in _SHUTDOWN_CALLBACKS:
        callback()
    _ASYNC_HTTP.close()


def _shutdown_at_exit() -> None:
    shutdown_networking()
    thread = _ASYNC_HTTP._thread
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=16.0)


atexit.register(_shutdown_at_exit)

async def http_json_async(url: str, params: dict[str, Any] | None=None, method: str='GET', headers: dict[str, str] | None=None, body: bytes | None=None, timeout: float=12.0, response_hook: Callable[[int, Any], None] | None=None) -> Any:
    return await _ASYNC_HTTP.request_json(url, params=params, method=method, headers=headers, body=body, timeout=timeout, response_hook=response_hook)

def run_async(coroutine: Coroutine[Any, Any, Any]) -> Any:
    return _ASYNC_HTTP.run(coroutine)

def submit_async(coroutine: Coroutine[Any, Any, Any]) -> concurrent.futures.Future[Any]:
    return _ASYNC_HTTP.submit(coroutine)

def http_json(url: str, params: dict[str, Any] | None=None, method: str='GET', headers: dict[str, str] | None=None, body: bytes | None=None, timeout: float=12.0, response_hook: Callable[[int, Any], None] | None=None) -> Any:
    """Compatibility wrapper over the shared async HTTP transport."""
    return run_async(http_json_async(url, params=params, method=method, headers=headers, body=body, timeout=timeout, response_hook=response_hook))

async def http_bytes_async(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 12.0,
    max_bytes: int = 2_000_000,
) -> bytes:
    return await _ASYNC_HTTP.request_bytes(
        url, headers=headers, timeout=timeout, max_bytes=max_bytes
    )

def http_bytes(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 12.0,
    max_bytes: int = 2_000_000,
) -> bytes:
    return run_async(
        http_bytes_async(url, headers=headers, timeout=timeout, max_bytes=max_bytes)
    )
from collections.abc import Callable
from typing import Any
from PySide6 import QtCore
from PySide6.QtCore import Signal
class TaskSignals(QtCore.QObject):
    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int)
    deferred = Signal(float)

    @QtCore.Slot(float)
    def schedule_retry(self, delay: float) -> None:
        if not self.cancelled.is_set():
            self.retry_timer.start(max(50, int(delay * 1000) + 25))

class ApiTask(QtCore.QRunnable):

    def __init__(self, function: Callable[[], Any], *, defer_rate_waits: bool=False, source: str=""):
        super().__init__()
        self.function = function
        self.signals = TaskSignals()
        self.defer_rate_waits = defer_rate_waits
        self.source = source or getattr(function, '__qualname__', type(function).__name__)
        self.read_results: dict[Any, Any] = {}
        self.retry_timer = QtCore.QTimer(self.signals)
        self.retry_timer.setSingleShot(True)
        self.signals.retry_timer = self.retry_timer
        self.pool: QtCore.QThreadPool | None = None
        self.valid: Callable[[], bool] | None = None
        self.cancelled = threading.Event()
        self.signals.cancelled = self.cancelled
        if defer_rate_waits:
            self.setAutoDelete(False)
            self.retry_timer.timeout.connect(self._retry)
            self.signals.deferred.connect(self.signals.schedule_retry)

    def _retry(self) -> None:
        if self.cancelled.is_set():
            return
        if self.valid is None or self.valid():
            (self.pool or QtCore.QThreadPool.globalInstance()).start(self)
        else:
            self.read_results.clear()
            self.signals.failed.emit('Request superseded.')

    @QtCore.Slot()
    def run(self) -> None:
        if self.cancelled.is_set():
            return
        defer_token = DEFER_RATE_WAITS.set(self.defer_rate_waits)
        source_token = REQUEST_SOURCE.set(self.source)
        cache_token = READ_RESULTS.set(self.read_results if self.defer_rate_waits else None)
        cancel_token = TASK_CANCELLED.set(self.cancelled)
        try:
            result = self.function()
        except RateLimitDeferred as exc:
            if not self.cancelled.is_set():
                self.signals.deferred.emit(exc.delay)
        except Exception as exc:
            if not isinstance(exc, RuntimeError):
                log.exception('Unexpected API task failure')
            self.read_results.clear()
            if not self.cancelled.is_set():
                self.signals.failed.emit(str(exc))
        else:
            self.read_results.clear()
            if not self.cancelled.is_set():
                self.signals.finished.emit(result)
        finally:
            TASK_CANCELLED.reset(cancel_token)
            READ_RESULTS.reset(cache_token)
            REQUEST_SOURCE.reset(source_token)
            DEFER_RATE_WAITS.reset(defer_token)

    def cancel(self) -> None:
        """Cancel unsent reads/retries; admitted placements keep their journal."""
        self.cancelled.set()
        self.retry_timer.stop()

def launch_task(function: Callable[[], Any], finished: Callable[[Any], None], failed: Callable[[str], None], pool: QtCore.QThreadPool | None=None, *, defer_rate_waits: bool=False, source: str="", valid: Callable[[], bool] | None=None) -> ApiTask:
    task = ApiTask(function, defer_rate_waits=defer_rate_waits, source=source)
    selected_pool = pool or QtCore.QThreadPool.globalInstance()
    task.pool = selected_pool
    task.valid = valid
    task.signals.finished.connect(finished)
    task.signals.failed.connect(failed)
    selected_pool.start(task)
    return task


import asyncio
import math
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import threading
import time
from collections import deque
from email.utils import parsedate_to_datetime
from typing import Any
from ..constants import BINANCE_WS_CONNECTION_LIMIT_5M
from ..models import safe_float
_BINANCE_ERROR_CODE = re.compile('(?<!\\d)(-\\d{3,5})(?!\\d)')
_UNCERTAIN_EXECUTION_CODES = {-1000, -1001, -1006, -1007}

def binance_error_code(error: Any) -> int | None:
    """Extract a Binance JSON error code without depending on one transport."""
    if isinstance(error, dict):
        value = error.get('code')
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
    value = getattr(error, 'binance_code', None)
    if value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    match = _BINANCE_ERROR_CODE.search(str(error))
    return int(match.group(1)) if match else None

def execution_outcome_uncertain(error: Any) -> bool:
    """True when Binance may have accepted a write despite transport failure."""
    if str(error).casefold().startswith('not sent:'):
        return False
    code = binance_error_code(error)
    if code in _UNCERTAIN_EXECUTION_CODES:
        return True
    message = str(error).casefold()
    # Binance explicitly guarantees these overload/service responses were not
    # executed. Other 503 variants and backend timeouts have unknown outcomes.
    if code == -1008 or (re.search(r'http\s+503\b', message) and any(
        text in message for text in ('service unavailable', 'internal error; unable to process your request')
    )):
        return False
    return bool(re.search(r'http\s+(?:408|5\d\d)\b', message) or any((token in message for token in ('network error', 'request timeout', 'request timed out', 'connection timed out', 'connection reset', 'connection aborted', 'remote end closed', 'connection closed', 'broken pipe', 'incomplete read', 'incompleteread', 'temporarily unavailable', 'unexpected response', 'execution status unknown', 'execution status is unknown', 'order link closed', 'socket closed'))))

class LocalRateLimitError(RuntimeError):
    """Rejected locally before transmission; never an uncertain execution."""


class BinanceRateLimiter:
    """One product's IP-weight, account-order and connection windows.

    Futures REST and WS/order weight are distinct under the current contract:
    https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-api-general-info
    REST order writes overlap the WS quota; Spot has its own limiter instance.
    """
    _HEADER_PATTERN = re.compile('x-mbx-(used-weight|order-count)-(\\d+)([smhd])', re.IGNORECASE)
    _INTERVAL_SECONDS = {'SECOND': 1, 'MINUTE': 60, 'HOUR': 3600, 'DAY': 86400}
    _SUFFIX_SECONDS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}

    def __init__(self, domain: str='FUTURES') -> None:
        self.domain = domain
        self._condition = threading.Condition()
        self._limits: dict[tuple[str, int], int] = {('REQUEST_WEIGHT', 60): 2400, ('WS_REQUEST_WEIGHT', 60): 2400, ('ORDERS', 10): 300, ('ORDERS', 60): 1200, ('FUTURES_DATA', 300): 1000, ('FUNDING_HISTORY', 300): 500}
        if domain == 'SPOT':
            self._limits = {('REQUEST_WEIGHT', 60): 6000}
        self._attribution: dict[int, dict[tuple[str, str], int]] = {}
        self._local_windows: dict[tuple[str, int], tuple[int, int]] = {}
        self._observed: dict[tuple[str, int], tuple[int, int]] = {}
        self._observed_windows: dict[tuple[str, int], int] = {}
        self._server_time_offset = 0.0
        self._server_time_known = False
        self._last_server_time_observed = 0.0
        self._blocked_until = 0.0
        self._ws_connections: deque[float] = deque()
        self._last_rate_log = 0.0
        self._last_stdout_warning = 0.0
        self._rate_handler: RotatingFileHandler | None = None
        self._log_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='nightwatch-rate-log')
        self._logging_closed = False
        register_network_shutdown(self.close)

    def close(self) -> None:
        with self._condition:
            if self._logging_closed:
                return
            self._logging_closed = True
            try:
                self._log_pool.submit(self._close_rate_log)
            except RuntimeError:
                # Executor shutdown precedes ordinary atexit callbacks.
                self._close_rate_log()
            self._log_pool.shutdown(wait=False)

    def _close_rate_log(self):
        if self._rate_handler is not None:
            self._rate_handler.close()
            self._rate_handler = None

    @staticmethod
    def _window_label(seconds: int) -> str:
        if seconds % 86400 == 0:
            return f'{seconds // 86400}d'
        if seconds % 3600 == 0:
            return f'{seconds // 3600}h'
        if seconds % 60 == 0:
            return f'{seconds // 60}m'
        return f'{seconds}s'

    @staticmethod
    def _percent(value: int, ceiling: int) -> float:
        return 100.0 * value / max(1, ceiling)

    def _snapshot_line(self) -> str:
        state = self.diagnostic_snapshot()
        weight = state['rest']
        line = f'{self.domain} REST WEIGHT {weight[0]}/{weight[1]}'
        if self.domain == 'FUTURES':
            ws, orders = state['ws'], state['orders']
            spot = state['spot_weight']
            line += (f' | FUTURES ORDER/WS WEIGHT {ws[0]}/{ws[1]}'
                     f' | SPOT WEIGHT {spot[0]}/{spot[1]}'
                     f' | ORDERS {orders[0]}/{orders[1]}'
                     f' | WS CONNECTIONS {state["connections_5m"]}/{state["connection_limit_5m"]}')
        if state['blocked_for']:
            line += f' | BLOCKED {state["blocked_for"]:.0f}s'
        top = sorted(state['subsystems'].items(), key=lambda item: item[1], reverse=True)[:5]
        return line + ' | CONSUMERS/60s ' + ', '.join(f'{name}={cost}' for name, cost in top)

    def _write_rate_log(self, force: bool=False) -> None:

        with self._condition:
            now = time.monotonic()
            if self._logging_closed or (not force and now - self._last_rate_log < 30.0):
                return
            self._last_rate_log = now
            line = self._snapshot_line()
            self._log_pool.submit(self._emit_rate_log, line)

    def _emit_rate_log(self, line):
        try:
            if self._rate_handler is None:
                root = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.StandardLocation.AppDataLocation)
                directory = os.path.join(root or os.path.expanduser('~/.nightwatch'), 'logs')
                os.makedirs(directory, exist_ok=True)
                self._rate_handler = RotatingFileHandler(os.path.join(directory, f'binance_{self.domain.lower()}_rate_limit.log'),
                                                        maxBytes=1024 * 1024, backupCount=2, encoding='utf-8', delay=True)
                self._rate_handler.setFormatter(logging.Formatter('%(asctime)s | %(message)s'))
            self._rate_handler.emit(logging.LogRecord(__name__, logging.INFO, __file__, 0, line, (), None))
        except OSError:
            log.debug('Rate diagnostics unavailable', exc_info=True)

    def _attribute(self, request_weight: int, ws_weight: int, source: str, endpoint: str) -> None:
        second = int(time.monotonic())
        for expired in tuple(self._attribution):
            if expired <= second - 60:
                del self._attribution[expired]
        bucket = self._attribution.setdefault(second, {})
        key = (source or REQUEST_SOURCE.get() or 'unclassified', endpoint or 'unspecified')
        bucket[key] = bucket.get(key, 0) + max(0, request_weight, ws_weight)
        self._write_rate_log()

    def _critical_stdout(self, message: str, force: bool=False) -> None:
        now = time.monotonic()
        if not force and now - self._last_stdout_warning < 30.0:
            return
        self._last_stdout_warning = now
        self._write_rate_log(force=True)
        print(f'RATE LIMIT CRITICAL | {message} | {self._snapshot_line()}', flush=True)

    @staticmethod
    def _normalized_type(value: Any) -> str:
        kind = str(value or '').upper()
        if kind == 'ORDER':
            return 'ORDERS'
        if kind == 'RAW_REQUEST':
            return 'RAW_REQUESTS'
        return kind

    def configure(self, rows: list[dict[str, Any]]) -> None:
        published: dict[tuple[str, int], int] = {}
        for row in rows:
            kind = self._normalized_type(row.get('rateLimitType'))
            base = self._INTERVAL_SECONDS.get(str(row.get('interval', '')).upper())
            interval_number = int(safe_float(row.get('intervalNum'), 1.0))
            limit = int(safe_float(row.get('limit')))
            if kind and base and (interval_number > 0) and (limit > 0):
                published[kind, base * interval_number] = limit
        if not published:
            return
        with self._condition:
            for key, limit in published.items():
                self._limits[key] = limit
                self._local_windows.setdefault(key, (-1, 0))
            self._condition.notify_all()

    def _estimated_server_wall(self) -> float:
        return time.time() + (self._server_time_offset if self._server_time_known else 0.0)

    @staticmethod
    def _window_id(key: tuple[str, int], wall: float) -> int:
        return int(wall // max(1, int(key[1])))

    def _window_wait(self, key: tuple[str, int], wall: float | None=None) -> float:
        seconds = max(1, int(key[1]))
        wall = self._estimated_server_wall() if wall is None else float(wall)
        remainder = wall % seconds
        return max(0.05, seconds - remainder)

    def _local_usage(self, key: tuple[str, int], wall: float) -> int:
        window = self._window_id(key, wall)
        stored_window, count = self._local_windows.get(key, (-1, 0))
        if stored_window != window:
            self._local_windows[key] = (window, 0)
            return 0
        return max(0, int(count))

    def _increment_local(self, key: tuple[str, int], cost: int, wall: float) -> None:
        window = self._window_id(key, wall)
        stored_window, count = self._local_windows.get(key, (-1, 0))
        if stored_window != window:
            count = 0
        self._local_windows[key] = (window, max(0, int(count)) + max(0, int(cost)))

    def _usage(self, key: tuple[str, int], now: float | None=None) -> int:
        del now
        wall = self._estimated_server_wall()
        window = self._window_id(key, wall)
        local = self._local_usage(key, wall)
        observed_count, observed_window = self._observed.get(key, (0, -1))
        observed = observed_count if observed_window == window else 0
        return max(local, observed)

    def _admit_costs(self, costs: list[tuple[tuple[str, int], int]]) -> None:
        wall = self._estimated_server_wall()
        for key, cost in costs:
            self._increment_local(key, cost, wall)
            observed_count, observed_window = self._observed.get(key, (0, -1))
            window = self._window_id(key, wall)
            if observed_window == window:
                self._observed[key] = (observed_count + cost, observed_window)

    def _costs(self, request_weight: int, order_count: int, endpoint_buckets: tuple[str, ...], ws_request_weight: int=0) -> list[tuple[tuple[str, int], int]]:
        output: list[tuple[tuple[str, int], int]] = []
        for key in self._limits:
            kind = key[0]
            cost = 0
            if kind == 'REQUEST_WEIGHT':
                cost = max(0, request_weight)
            elif kind == 'RAW_REQUESTS':
                cost = 1
            elif kind == 'ORDERS':
                cost = max(0, order_count)
            elif kind == 'WS_REQUEST_WEIGHT':
                cost = max(0, ws_request_weight)
            elif kind in endpoint_buckets:
                cost = 1
            if cost:
                output.append((key, cost))
        return output

    @staticmethod
    def _normalized_priority(priority: str) -> str:
        value = str(priority or 'live').lower()
        return value if value in {'manual', 'safety', 'reconciliation', 'repair', 'live', 'analytics', 'background'} else 'live'

    def _admission_ceiling(self, key: tuple[str, int], cost: int, priority: str) -> int:
        published = max(1, int(self._limits[key]))

        fraction = {'manual': 1.0, 'safety': 1.0, 'repair': .95,
                    'live': .85, 'reconciliation': .85, 'analytics': .70, 'background': .50}[self._normalized_priority(priority)]
        if key[0] == 'ORDERS':
            return published
        return max(int(cost), int(published * fraction))

    def estimated_wait(self, request_weight: int, order_count: int=0, endpoint_buckets: tuple[str, ...]=(), priority: str='live', ws_request_weight: int=0) -> float:
        """Return the current fixed-window admission delay without reserving weight."""
        priority = self._normalized_priority(priority)
        with self._condition:
            now = time.monotonic()
            waits: list[float] = []
            if self._blocked_until > now:
                waits.append(self._blocked_until - now)
            costs = self._costs(request_weight, order_count, endpoint_buckets, ws_request_weight)
            wall = self._estimated_server_wall()
            for key, cost in costs:
                published = max(1, int(self._limits[key]))
                if cost > published:
                    return math.inf
                ceiling = self._admission_ceiling(key, cost, priority)
                if self._usage(key) + cost > ceiling:
                    waits.append(self._window_wait(key, wall))
            return max(0.0, max(waits)) if waits else 0.0

    def acquire(self, request_weight: int, order_count: int=0, endpoint_buckets: tuple[str, ...]=(), priority: str='live', wait: bool=True, ws_request_weight: int=0, *, source: str='', endpoint: str='') -> None:
        priority = self._normalized_priority(priority)
        with self._condition:
            while True:
                now = time.monotonic()
                waits: list[float] = []
                if self._blocked_until > now:
                    waits.append(self._blocked_until - now)
                costs = self._costs(request_weight, order_count, endpoint_buckets, ws_request_weight)
                wall = self._estimated_server_wall()
                for key, cost in costs:
                    published = max(1, int(self._limits[key]))
                    if cost > published:
                        raise LocalRateLimitError('Request exceeds the Binance published rate limit.')
                    ceiling = self._admission_ceiling(key, cost, priority)
                    if self._usage(key) + cost <= ceiling:
                        continue
                    waits.append(self._window_wait(key, wall))
                if not waits:
                    self._admit_costs(costs)
                    self._attribute(request_weight, ws_request_weight, source, endpoint)
                    return
                if not wait:
                    self._critical_stdout('BINANCE RATE WINDOW FULL')
                    raise LocalRateLimitError('Binance API rate window is full; wait for the exchange window to reset before submitting the order.')
                self._critical_stdout('BINANCE RATE WINDOW FULL')
                self._condition.wait(timeout=max(0.05, min(waits)))

    async def acquire_async(self, request_weight: int, order_count: int=0, endpoint_buckets: tuple[str, ...]=(), priority: str='live', wait: bool=True, ws_request_weight: int=0, *, source: str='', endpoint: str='') -> None:
        """Async admission against Binance's published fixed windows."""
        priority = self._normalized_priority(priority)
        while True:
            with self._condition:
                now = time.monotonic()
                waits: list[float] = []
                if self._blocked_until > now:
                    waits.append(self._blocked_until - now)
                costs = self._costs(request_weight, order_count, endpoint_buckets, ws_request_weight)
                wall = self._estimated_server_wall()
                for key, cost in costs:
                    published = max(1, int(self._limits[key]))
                    if cost > published:
                        raise LocalRateLimitError('Request exceeds the Binance published rate limit.')
                    ceiling = self._admission_ceiling(key, cost, priority)
                    if self._usage(key) + cost <= ceiling:
                        continue
                    waits.append(self._window_wait(key, wall))
                if not waits:
                    self._admit_costs(costs)
                    self._attribute(request_weight, ws_request_weight, source, endpoint)
                    return
                if not wait:
                    self._critical_stdout('BINANCE RATE WINDOW FULL')
                    raise LocalRateLimitError('Binance API rate window is full; wait for the exchange window to reset before submitting the order.')
                self._critical_stdout('BINANCE RATE WINDOW FULL')
                delay = max(0.05, min(waits))
            if DEFER_RATE_WAITS.get():
                raise RateLimitDeferred(delay)
            await asyncio.sleep(min(delay, 0.25))

    def observe(self, status: int, headers: Any) -> None:
        now = time.monotonic()
        normalized = {str(key).lower(): str(value) for key, value in headers.items()}
        critical: str | None = None
        server_time_from_header = False
        try:
            server_now = parsedate_to_datetime(normalized.get('date', '')).timestamp()
            server_time_from_header = True
        except (TypeError, ValueError, OverflowError):
            server_now = self._estimated_server_wall()
        with self._condition:
            if server_time_from_header:
                self._observe_server_time(server_now)
            if status in {418, 429}:
                retry_after = max(1.0, safe_float(normalized.get('retry-after'), 60.0))
                self._blocked_until = max(self._blocked_until, now + retry_after)
                critical = f'HTTP {status} · retry-after {retry_after:.0f}s'
            for header, value in normalized.items():
                match = self._HEADER_PATTERN.fullmatch(header)
                if not match:
                    continue
                kind = 'REQUEST_WEIGHT' if match.group(1).lower() == 'used-weight' else 'ORDERS'
                seconds = int(match.group(2)) * self._SUFFIX_SECONDS[match.group(3).lower()]
                key = (kind, seconds)
                if key in self._limits:
                    observed = max(0, int(safe_float(value)))
                    observed = self._record_observed(key, observed, now, server_now)
                    if self._percent(observed, self._limits[key]) >= 90.0:
                        critical = f'{kind}/{self._window_label(seconds)} {observed}/{self._limits[key]}'
            self._condition.notify_all()
        if critical:
            self._critical_stdout(critical, force=status in {418, 429})

    def diagnostic_snapshot(self) -> dict[str, Any]:
        """Small read-only snapshot used by the in-app Developer panel."""
        now = time.monotonic()
        with self._condition:

            def state(key: tuple[str, int], default_limit: int) -> tuple[int, int]:
                limit = max(1, int(self._limits.get(key, default_limit)))
                return (int(self._usage(key, now)), limit)
            rest_usage, rest_limit = state(('REQUEST_WEIGHT', 60), 2400)
            ws_usage, ws_limit = state(('WS_REQUEST_WEIGHT', 60), 2400)
            order_usage, order_limit = state(('ORDERS', 60), 1200)
            while self._ws_connections and now - self._ws_connections[0] >= 300.0:
                self._ws_connections.popleft()
            connections = len(self._ws_connections)
            blocked_for = max(0.0, self._blocked_until - now)
            endpoints: dict[str, int] = {}
            subsystems: dict[str, int] = {}
            for second, bucket in self._attribution.items():
                if second <= int(now) - 60:
                    continue
                for (source, endpoint), cost in bucket.items():
                    endpoints[endpoint] = endpoints.get(endpoint, 0) + cost
                    subsystems[source] = subsystems.get(source, 0) + cost
        spot = SPOT_RATE_LIMITER.diagnostic_snapshot()['rest'] if self.domain == 'FUTURES' else (rest_usage, rest_limit)
        return {'futures_rest_weight': (rest_usage, rest_limit), 'futures_order_ws_weight': (ws_usage, ws_limit), 'spot_weight': spot, 'endpoints': endpoints, 'subsystems': subsystems, 'rest': (rest_usage, rest_limit), 'ws': (ws_usage, ws_limit), 'orders': (order_usage, order_limit), 'connections_5m': connections, 'connection_limit_5m': int(BINANCE_WS_CONNECTION_LIMIT_5M), 'blocked_for': blocked_for}

    def _observe_server_time(self, server_now: float) -> None:
        """Advance the Binance wall-clock estimate without accepting stale rewinds.

        HTTP responses may complete out of order.  A delayed response can carry an
        older ``Date`` header even when its rate counter is correctly rejected as
        stale.  Clock reconciliation therefore has its own monotonic server-time
        boundary and is independent from rate-window accounting.
        """
        try:
            server_now = float(server_now)
        except (TypeError, ValueError, OverflowError):
            return
        if server_now <= 0.0:
            return
        if self._server_time_known and server_now <= self._last_server_time_observed:
            return
        self._server_time_offset = server_now - time.time()
        self._server_time_known = True
        self._last_server_time_observed = server_now

    def _record_observed(self, key, count, now, server_now):
        del now
        server_now = float(server_now)
        window = self._window_id(key, server_now)
        prior_window = self._observed_windows.get(key, -1)
        previous, previous_window = self._observed.get(key, (0, -1))
        if window < prior_window:
            return previous
        if window == prior_window and previous_window == window:
            count = max(int(count), int(previous))
        else:
            self._local_windows[key] = (window, 0)
        self._observed_windows[key] = window
        self._observed[key] = (max(0, int(count)), window)
        return max(0, int(count))

    def observe_websocket_limits(self, rows: list[dict[str, Any]]) -> None:
        now = time.monotonic()
        critical: str | None = None
        with self._condition:
            for row in rows:
                kind = self._normalized_type(row.get('rateLimitType'))
                if kind == 'REQUEST_WEIGHT':
                    kind = 'WS_REQUEST_WEIGHT'
                base = self._INTERVAL_SECONDS.get(str(row.get('interval', '')).upper())
                interval_number = int(safe_float(row.get('intervalNum'), 1.0))
                limit = int(safe_float(row.get('limit')))
                count = int(safe_float(row.get('count')))
                if not kind or not base or interval_number <= 0 or (limit <= 0):
                    continue
                key = (kind, base * interval_number)
                self._limits[key] = limit
                self._local_windows.setdefault(key, (-1, 0))
                observed = max(0, count)
                observed = self._record_observed(key, observed, now, self._estimated_server_wall())
                if self._percent(observed, limit) >= 90.0:
                    critical = f'{kind}/{self._window_label(key[1])} {observed}/{limit}'
            self._condition.notify_all()
        if critical:
            self._critical_stdout(critical)

    def reserve_websocket_connection(self, *, api: bool=True) -> float:
        now = time.monotonic()
        with self._condition:
            if self._blocked_until > now:
                return self._blocked_until - now
            while self._ws_connections and now - self._ws_connections[0] >= 300.0:
                self._ws_connections.popleft()
            ceiling = int(BINANCE_WS_CONNECTION_LIMIT_5M)
            weight_key = ('WS_REQUEST_WEIGHT', 60)
            weight_ceiling = int(self._limits.get(weight_key, 2400))
            weight_usage = self._usage(weight_key)
            cost = 5 if api else 0
            if len(self._ws_connections) < ceiling and (not api or weight_usage + cost <= weight_ceiling):
                self._ws_connections.append(now)
                if cost:
                    self._admit_costs([(weight_key, cost)])
                    self._attribute(0, cost, "connections", "WS API handshake")
                return 0.0
            connection_wait = 300.0 - (now - self._ws_connections[0]) if len(self._ws_connections) >= ceiling and self._ws_connections else 0.0
            weight_wait = self._window_wait(weight_key) if api and weight_usage + cost > weight_ceiling else 0.0
            return max(0.25, connection_wait, weight_wait)
SPOT_RATE_LIMITER = BinanceRateLimiter("SPOT")
BINANCE_RATE_LIMITER = BinanceRateLimiter()
import csv
import io
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from ..constants import APP_NAME
from ..database import AppDatabase
VISION_BUCKET = 'https://s3-ap-northeast-1.amazonaws.com/data.binance.vision'
VISION_FILES = 'https://data.binance.vision/'

def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

class BinanceVisionArchive:

    def __init__(self, database: AppDatabase):
        self.database = database

    @staticmethod
    def _read(url: str, timeout: float=30.0) -> bytes:
        request = urllib.request.Request(url, headers={'User-Agent': f'{APP_NAME}/1.0'})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            ceiling = 64 * 1024 * 1024
            chunks = bytearray()
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    return bytes(chunks)
                if len(chunks) + len(chunk) > ceiling:
                    raise RuntimeError("Binance Vision archive exceeds the 64 MiB download limit.")
                chunks.extend(chunk)

    def _archive_keys(self, prefix: str, stopped: Callable[[], bool]) -> list[str]:
        keys: list[str] = []
        continuation = ''
        while not stopped():
            params = {'list-type': '2', 'prefix': prefix, 'max-keys': '1000'}
            if continuation:
                params['continuation-token'] = continuation
            payload = self._read(f'{VISION_BUCKET}?{urllib.parse.urlencode(params)}')
            root = ET.fromstring(payload)
            namespace = {'s3': 'http://s3.amazonaws.com/doc/2006-03-01/'}
            keys.extend((str(node.text) for node in root.findall('s3:Contents/s3:Key', namespace) if node.text and node.text.endswith('.zip')))
            truncated = root.findtext('s3:IsTruncated', 'false', namespace)
            continuation = root.findtext('s3:NextContinuationToken', '', namespace)
            if truncated.lower() != 'true' or not continuation:
                break
        return keys

    def _csv_rows(self, archive_key: str) -> list[dict[str, str]]:
        payload = self._read(VISION_FILES + archive_key)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            if sum(info.file_size for info in archive.infolist()) > 256 * 1024 * 1024:
                raise RuntimeError('Binance Vision archive expands beyond the 256 MiB import limit.')
            if archive.testzip() is not None:
                raise RuntimeError(f'Damaged Binance Vision archive: {archive_key}')
            names = [name for name in archive.namelist() if name.endswith('.csv')]
            if not names:
                return []
            with archive.open(names[0]) as raw:
                text = io.TextIOWrapper(raw, encoding='utf-8-sig', newline='')
                return [dict(row) for row in csv.DictReader(text)]

    def import_open_interest(self, symbol: str, stopped: Callable[[], bool], status: Callable[[str], None]) -> tuple[int, int]:
        prefix = f'data/futures/um/daily/metrics/{symbol}/'
        keys = self._archive_keys(prefix, stopped)
        imported = self.database.imported_archive_keys(prefix)
        imported_files = 0
        imported_rows = 0
        for position, archive_key in enumerate(keys, 1):
            if stopped():
                break
            if archive_key in imported:
                continue
            status(f'Open interest archive {position:,} of {len(keys):,}')
            events: list[tuple[int, dict[str, Any]]] = []
            for row in self._csv_rows(archive_key):
                stamp = str(row.get('create_time', ''))[:19]
                try:
                    event_time = int(datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp() * 1000)
                except ValueError:
                    continue
                if event_time % 900000:
                    continue
                oi_value = _number(row.get('sum_open_interest_value'))
                oi_amount = _number(row.get('sum_open_interest'))
                if oi_value <= 0 and oi_amount <= 0:
                    continue
                events.append((event_time, {'symbol': symbol, 'sumOpenInterest': oi_amount, 'sumOpenInterestValue': oi_value, 'timestamp': event_time, 'countTopTraderLongShortRatio': _number(row.get('count_toptrader_long_short_ratio')), 'sumTopTraderLongShortRatio': _number(row.get('sum_toptrader_long_short_ratio')), 'countLongShortRatio': _number(row.get('count_long_short_ratio')), 'sumTakerLongShortVolRatio': _number(row.get('sum_taker_long_short_vol_ratio'))}))
            self.database.cache_market_event_page(symbol, 'open_interest', events, False)
            self.database.mark_archive_imported(archive_key, 'open_interest', symbol, len(events))
            imported_files += 1
            imported_rows += len(events)
            time.sleep(0.01)
        return (imported_files, imported_rows)

    def import_premium_index(self, symbol: str, stopped: Callable[[], bool], status: Callable[[str], None]) -> tuple[int, int]:
        prefix = f'data/futures/um/monthly/premiumIndexKlines/{symbol}/15m/'
        keys = self._archive_keys(prefix, stopped)
        imported = self.database.imported_archive_keys(prefix)
        imported_files = 0
        imported_rows = 0
        for position, archive_key in enumerate(keys, 1):
            if stopped():
                break
            if archive_key in imported:
                continue
            status(f'Premium-index archive {position:,} of {len(keys):,}')
            events: list[tuple[int, dict[str, Any]]] = []
            for row in self._csv_rows(archive_key):
                event_time = int(_number(row.get('open_time')))
                premium = _number(row.get('close'))
                if event_time <= 0:
                    continue
                events.append((event_time, {'openTime': event_time, 'premium': premium}))
            self.database.cache_market_event_page(symbol, 'premium_index', events, False)
            self.database.mark_archive_imported(archive_key, 'premium_index', symbol, len(events))
            imported_files += 1
            imported_rows += len(events)
            time.sleep(0.01)
        return (imported_files, imported_rows)
import asyncio
import csv
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.parse
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
import numpy as np
from ..constants import CONDITIONAL_ORDER_TYPES, HISTORY_PAGE_LIMIT, MAIN_REST, MAX_CHART_CANDLES, TEST_REST
from ..models import Candle, shift_candle_time
from ..models import api_period, safe_float
_KLINE_INFLIGHT: dict[tuple[Any, ...], asyncio.Task[Any]] = {}
_BACKGROUND_GATE: asyncio.Semaphore | None = None

async def _background_fetch(fetch: Callable[[], Awaitable[Any]]) -> Any:
    """Keep rate-window waits outside the bounded HTTP concurrency gate."""
    global _BACKGROUND_GATE
    if _BACKGROUND_GATE is None:
        _BACKGROUND_GATE = asyncio.Semaphore(4)
    deferred = DEFER_RATE_WAITS.get()
    while True:
        try:
            async with _BACKGROUND_GATE:
                token = DEFER_RATE_WAITS.set(True)
                try:
                    return await fetch()
                finally:
                    DEFER_RATE_WAITS.reset(token)
        except RateLimitDeferred as exc:
            if deferred:
                raise
            await asyncio.sleep(min(exc.delay, 0.25))


async def _shared_kline_fetch(key: tuple[Any, ...], fetch: Callable[[], Awaitable[Any]]) -> Any:
    """Share one in-flight public kline HTTP exchange among exact waiters.

    Every waiter shields the shared task, so cancellation of one caller cannot
    cancel an already-admitted request needed by another caller.  Completed
    tasks remove themselves from the ledger immediately; this is in-flight
    coalescing only, not a second response cache.
    """
    task = _KLINE_INFLIGHT.get(key)
    if task is None or task.done():
        task = asyncio.create_task(fetch())
        _KLINE_INFLIGHT[key] = task

        def cleanup(done: asyncio.Task[Any]) -> None:
            if _KLINE_INFLIGHT.get(key) is done:
                _KLINE_INFLIGHT.pop(key, None)
            if not done.cancelled():
                try:
                    done.exception()
                except asyncio.CancelledError:
                    pass
        task.add_done_callback(cleanup)
    else:
        pass
    return await asyncio.shield(task)

class _ServerClockState:

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.server_time_ms = 0
        self.synced_at = 0.0
        self.last_rtt_ms = 0.0
        self.sync_count = 0
        self.last_error = ''
        self.refresh_future = None
_SERVER_CLOCKS: dict[str, _ServerClockState] = {}
_SERVER_CLOCKS_LOCK = threading.Lock()

def _server_clock(base: str) -> _ServerClockState:
    with _SERVER_CLOCKS_LOCK:
        state = _SERVER_CLOCKS.get(base)
        if state is None:
            state = _ServerClockState()
            _SERVER_CLOCKS[base] = state
        return state

class BinanceRest:

    def __init__(self, testnet: bool=False):
        self.testnet = testnet
        self.base = TEST_REST if testnet else MAIN_REST
        self._time_state = _server_clock(self.base)
        self._position_mode_cache: tuple[str, float, dict[str, Any]] | None = None
        self._position_mode_lock = threading.Lock()
        self._position_mode_async_lock: asyncio.Lock | None = None
        self._spot_symbols_cache: frozenset[str] | None = None
        self._spot_symbols_cached_at = 0.0
        self._spot_symbols_lock: asyncio.Lock | None = None
        self.market_data_api_key = os.getenv('BINANCE_API_KEY', '').strip()
        self._ensure_time_sync_loop()

    def set_market_data_api_key(self, api_key: str) -> None:
        """Set the key used by Binance MARKET_DATA endpoints that require a header."""
        self.market_data_api_key = str(api_key or '').strip()

    @staticmethod
    def _request_weight(path: str, params: dict[str, Any] | None) -> int:
        params = params or {}
        if path == '/fapi/v1/positionSide/dual':
            return 30
        if path in {'/fapi/v1/accountConfig', '/fapi/v1/symbolConfig'}:
            return 5
        if path in {'/fapi/v1/klines', '/fapi/v1/continuousKlines', '/fapi/v1/indexPriceKlines', '/fapi/v1/markPriceKlines', '/fapi/v1/premiumIndexKlines'}:
            limit = max(1, int(safe_float(params.get('limit'), 500.0)))
            if limit < 100:
                return 1
            if limit < 500:
                return 2
            if limit <= 1000:
                return 5
            return 10
        if path == '/fapi/v1/depth':
            limit = max(5, int(safe_float(params.get('limit'), 500.0)))
            if limit <= 50:
                return 2
            if limit <= 100:
                return 5
            if limit <= 500:
                return 10
            return 20
        if path == '/fapi/v1/ticker/24hr':
            return 1 if params.get('symbol') else 40
        if path == '/fapi/v1/premiumIndex':
            return 1 if params.get('symbol') else 10
        if path in {'/fapi/v1/openOrders', '/fapi/v1/openAlgoOrders'}:
            return 1 if params.get('symbol') else 40
        if path in {'/fapi/v1/allOpenOrders', '/fapi/v1/algoOpenOrders'}:
            return 1
        if path == '/fapi/v1/batchOrders':
            return 5
        if path.startswith('/futures/data/'):
            return 0
        if path == '/fapi/v1/fundingInfo':
            return 0
        if path in {'/fapi/v1/order', '/fapi/v1/algoOrder', '/fapi/v1/leverage', '/fapi/v1/marginType', '/fapi/v1/listenKey'}:
            return 1
        if path in {'/fapi/v3/account', '/fapi/v3/balance', '/fapi/v3/positionRisk', '/fapi/v2/account', '/fapi/v2/balance', '/fapi/v2/positionRisk', '/fapi/v1/userTrades'}:
            return 5
        if path in {'/fapi/v1/exchangeInfo', '/fapi/v1/time', '/fapi/v1/openInterest', '/fapi/v1/fundingRate'}:
            return 1
        return 20

    @staticmethod
    def _endpoint_buckets(path: str) -> tuple[str, ...]:
        buckets: list[str] = []
        if path.startswith('/futures/data/'):
            buckets.append('FUTURES_DATA')
        if path in {'/fapi/v1/fundingRate', '/fapi/v1/fundingInfo'}:
            buckets.append('FUNDING_HISTORY')
        return tuple(buckets)

    async def _klines_shared(self, params: dict[str, Any], priority: str) -> Any:
        return await self._request_async('/fapi/v1/klines', params=params, priority=priority)

    def _request(self, path: str, params: dict[str, Any] | None=None, method: str='GET', headers: dict[str, str] | None=None, body: bytes | None=None, priority: str='live', order_count: int=0) -> Any:
        if method.upper() == 'GET' and path == '/fapi/v1/klines':
            return run_async(self._klines_shared(params or {}, priority))
        return run_async(self._request_async(path, params=params, method=method, headers=headers, body=body, priority=priority, order_count=order_count))

    async def _request_async(self, path: str, params: dict[str, Any] | None=None, method: str='GET', headers: dict[str, str] | None=None, body: bytes | None=None, priority: str='live', order_count: int=0) -> Any:
        cache = READ_RESULTS.get()
        key = (self.base, path, tuple(sorted((str(k), str(v)) for k, v in (params or {}).items())))
        cacheable = method.upper() == 'GET' and not headers and path != '/fapi/v1/time'
        if cacheable and cache is not None and key in cache:
            return cache[key]

        async def fetch() -> Any:
            if priority in {'background', 'analytics'}:
                return await _background_fetch(lambda: self._request_admitted(path, params, method, headers, body, priority, order_count))
            return await self._request_admitted(path, params, method, headers, body, priority, order_count)

        if cacheable and path == '/fapi/v1/klines':


            result = await _shared_kline_fetch((*key, priority), fetch)
        else:
            result = await fetch()
        if cacheable and cache is not None:
            cache[key] = result
        return result

    async def _request_admitted(self, path: str, params: dict[str, Any] | None=None, method: str='GET', headers: dict[str, str] | None=None, body: bytes | None=None, priority: str='live', order_count: int=0) -> Any:
        buckets = self._endpoint_buckets(path)
        weight = self._request_weight(path, params)
        if method.upper() in {'POST', 'PUT'} and path in {'/fapi/v1/order', '/fapi/v1/algoOrder'}:
            weight = 0
        if method.upper() in {'POST', 'PUT', 'DELETE'} and path in {'/fapi/v1/order', '/fapi/v1/algoOrder', '/fapi/v1/batchOrders', '/fapi/v1/allOpenOrders', '/fapi/v1/algoOpenOrders'}:
            buckets += ('WS_REQUEST_WEIGHT',)
        if path == '/fapi/v1/depth':
            priority = 'repair'
        source = REQUEST_SOURCE.get() or ('order execution' if method.upper() != 'GET' else 'account reconciliation' if priority == 'manual' else 'chart history' if path.endswith('/klines') else 'market overview')
        await BINANCE_RATE_LIMITER.acquire_async(weight, order_count, buckets, priority, wait=priority not in {'manual', 'reconciliation'}, ws_request_weight=weight if 'WS_REQUEST_WEIGHT' in buckets else 0, source=source, endpoint=path)
        return await http_json_async(self.base + path, params=params, method=method, headers=headers, body=body, response_hook=BINANCE_RATE_LIMITER.observe)

    def get(self, path: str, params: dict[str, Any] | None=None, priority: str='live') -> Any:
        return self._request(path, params=params, priority=priority)

    @staticmethod
    def _encoded_params(params: dict[str, Any]) -> dict[str, str]:
        encoded: dict[str, str] = {}
        for key, value in params.items():
            if value is None or value == '':
                continue
            if isinstance(value, bool):
                encoded[key] = 'true' if value else 'false'
            elif isinstance(value, (dict, list, tuple)):
                encoded[key] = json.dumps(value, separators=(',', ':'))
            else:
                encoded[key] = str(value)
        return encoded

    async def _sync_time_once_async(self, *, priority: str='background') -> int:
        started = time.monotonic()
        payload = await self._request_async('/fapi/v1/time', priority=priority)
        finished = time.monotonic()
        if not isinstance(payload, dict) or type(payload.get('serverTime')) is not int or payload['serverTime'] <= 0:
            raise RuntimeError('Unexpected response: Binance omitted a valid serverTime.')
        server_time = payload['serverTime']
        estimate = server_time + round((finished - started) * 500)
        rtt_ms = max(0.0, (finished - started) * 1000.0)
        state = self._time_state
        with state.lock:
            state.server_time_ms = estimate
            state.synced_at = finished
            state.last_rtt_ms = rtt_ms
            state.sync_count += 1
            state.last_error = ''
        return estimate

    async def _time_sync_loop(self) -> None:
        while True:
            try:
                await self._sync_time_once_async(priority='background')
            except Exception as exc:
                state = self._time_state
                error = f'{type(exc).__name__}: {exc}'
                with state.lock:
                    state.last_error = error
            await asyncio.sleep(30.0)

    def _ensure_time_sync_loop(self) -> None:
        state = self._time_state
        with state.lock:
            future = state.refresh_future
            if future is not None and (not future.done()):
                return
            state.refresh_future = submit_async(self._time_sync_loop())

    def sync_time(self, force: bool=False) -> int:
        """Return server-adjusted time without normal-path network I/O."""
        self._ensure_time_sync_loop()
        if force:
            run_async(self._sync_time_once_async(priority='manual'))
        return self.cached_timestamp_ms()

    def timestamp_ms(self) -> int:
        self._ensure_time_sync_loop()
        return self.cached_timestamp_ms()

    def has_fresh_time_offset(self, maximum_age: float=60.0) -> bool:
        state = self._time_state
        with state.lock:
            return bool(state.synced_at and time.monotonic() - state.synced_at <= maximum_age)

    def cached_timestamp_ms(self) -> int:
        """Return synchronized time without performing network I/O."""
        state = self._time_state
        with state.lock:
            if not state.synced_at:
                return int(time.time() * 1000)
            return state.server_time_ms + round((time.monotonic() - state.synced_at) * 1000)

    def invalidate_time_sync(self) -> None:
        state = self._time_state
        with state.lock:
            state.server_time_ms = 0
            state.synced_at = 0.0

    def server_clock_status(self) -> dict[str, Any]:
        """Return read-only health for the cached Binance clock estimator.

        Epoch quantities are compared only with epoch quantities. Elapsed age
        uses the monotonic clock. This method performs no network I/O.
        """
        state = self._time_state
        now_mono = time.monotonic()
        now_wall_ms = time.time() * 1000.0
        with state.lock:
            synced_at = float(state.synced_at)
            server_time_ms = int(state.server_time_ms)
            rtt_ms = float(state.last_rtt_ms)
            sync_count = int(state.sync_count)
            last_error = str(state.last_error or '')
        if synced_at <= 0.0 or server_time_ms <= 0:
            return {'synced': False, 'fresh': False, 'age_s': None, 'offset_ms': None, 'rtt_ms': rtt_ms if rtt_ms > 0.0 else None, 'sync_count': sync_count, 'last_error': last_error or 'never synced'}
        age_s = max(0.0, now_mono - synced_at)
        estimated_server_ms = server_time_ms + round(age_s * 1000.0)
        offset_ms = estimated_server_ms - now_wall_ms
        fresh = age_s <= 90.0
        return {'synced': True, 'fresh': fresh, 'age_s': age_s, 'offset_ms': offset_ms, 'rtt_ms': rtt_ms, 'sync_count': sync_count, 'last_error': last_error}

    def signed_request(self, api_key: str, api_secret: str, path: str, params: dict[str, Any] | None=None, method: str='GET', order_count: int=0, *, before_send: Callable[[], None] | None = None) -> Any:
        return run_async(self._signed_request_async(api_key, api_secret, path, params, method, order_count, before_send=before_send))

    async def _signed_request_async(self, api_key: str, api_secret: str, path: str, params: dict[str, Any] | None=None, method: str='GET', order_count: int=0, *, priority: str='manual', before_send: Callable[[], None] | None = None) -> Any:
        values = self._encoded_params(params or {})
        values.setdefault('recvWindow', '5000')
        headers = {'X-MBX-APIKEY': api_key, 'Content-Type': 'application/x-www-form-urlencoded'}

        async def send() -> Any:
            if before_send is not None:
                before_send()
            if not self.has_fresh_time_offset():
                try:
                    await self._sync_time_once_async(priority=priority)
                except (RuntimeError, TimeoutError, OSError) as exc:
                    raise RuntimeError(f'Not sent: Binance clock synchronization failed: {exc}') from exc
            if before_send is not None:
                before_send()
            values['timestamp'] = str(self.cached_timestamp_ms())
            query = urllib.parse.urlencode(values)
            signature = hmac.new(api_secret.encode('utf-8'), query.encode('utf-8'), hashlib.sha256).hexdigest()
            if method.upper() != 'GET':
                return await self._request_async(path, method=method.upper(), headers=headers, body=f'{query}&signature={signature}'.encode('utf-8'), priority=priority, order_count=order_count)
            return await self._request_async(path, params={**values, 'signature': signature}, method='GET', headers=headers, priority=priority, order_count=order_count)
        try:
            return await send()
        except RuntimeError as exc:
            message = str(exc).lower()
            if '-1021' not in message and 'timestamp for this request' not in message:
                raise
            self.invalidate_time_sync()
            return await send()

    def exchange_info(self) -> dict[str, Any]:
        exchange = self.get('/fapi/v1/exchangeInfo')
        BINANCE_RATE_LIMITER.configure(list(exchange.get('rateLimits', [])))
        try:
            tickers = self.get('/fapi/v1/ticker/24hr', priority='background')
        except (RuntimeError, TimeoutError, OSError):
            tickers = []
        return {'exchange': exchange, 'tickers': tickers}

    def market_tickers(self) -> list[dict[str, Any]]:
        return self.get('/fapi/v1/ticker/24hr', priority='background')

    async def _spot_symbols_async(self) -> frozenset[str]:
        """Return Binance Spot TRADING symbols, cached for six hours."""
        if self.testnet:
            return frozenset()
        now = time.monotonic()
        cached = self._spot_symbols_cache
        if cached is not None and now - self._spot_symbols_cached_at < 6 * 60 * 60:
            return cached
        if self._spot_symbols_lock is None:
            self._spot_symbols_lock = asyncio.Lock()
        async with self._spot_symbols_lock:
            now = time.monotonic()
            cached = self._spot_symbols_cache
            if cached is not None and now - self._spot_symbols_cached_at < 6 * 60 * 60:
                return cached
            await SPOT_RATE_LIMITER.acquire_async(20, 0, (), 'background', wait=True, source='Spot universe', endpoint='/api/v3/exchangeInfo')
            payload = await http_json_async('https://data-api.binance.vision/api/v3/exchangeInfo', response_hook=SPOT_RATE_LIMITER.observe)
            SPOT_RATE_LIMITER.configure(list(payload.get('rateLimits', [])))
            symbols = frozenset((str(row.get('symbol') or '') for row in payload.get('symbols', []) if isinstance(row, dict) and row.get('status') == 'TRADING' and row.get('symbol')))
            self._spot_symbols_cache = symbols
            self._spot_symbols_cached_at = now
            return symbols

    async def _spot_klines(self, params: dict[str, Any]) -> Any:
        cache = READ_RESULTS.get()
        key = ('spot', '/api/v3/klines', tuple(sorted((str(k), str(v)) for k, v in params.items())))
        if cache is not None and key in cache:
            return cache[key]
        async def fetch() -> Any:
            await SPOT_RATE_LIMITER.acquire_async(2, priority='background', source=REQUEST_SOURCE.get() or 'Spot comparison/history', endpoint='/api/v3/klines')
            return await http_json_async('https://data-api.binance.vision/api/v3/klines', params=params, response_hook=SPOT_RATE_LIMITER.observe)

        rows = await _shared_kline_fetch(key, lambda: _background_fetch(fetch))
        if cache is not None:
            cache[key] = rows
        return rows

    def spot_hour_history(self, symbol: str, end_ms: int, hours: int=28) -> list[Candle]:
        """Public exact-symbol Spot history, skipping futures-only symbols."""
        if self.testnet:
            return []

        async def fetch_rows() -> Any:
            spot_symbols = await self._spot_symbols_async()
            if symbol not in spot_symbols:
                return []
            return await self._spot_klines({'symbol': symbol, 'interval': '1h', 'limit': hours, 'startTime': end_ms - hours * 3600000, 'endTime': end_ms - 1})
        rows = run_async(fetch_rows())
        return [Candle.from_rest(row) for row in rows if len(row) > 7 and int(row[0]) + 3600000 <= end_ms]

    def sector_snapshot_batch(self, requests: dict[str, dict[str, Any]], end_15m: int, end_hour: int) -> dict[str, dict[str, Any]]:
        """Fetch only Sector components still missing after persistent-cache reads."""
        if not requests:
            return {}
        day_end = int(end_hour // 86400000) * 86400000

        async def collect() -> dict[str, dict[str, Any]]:
            gate = asyncio.Semaphore(6)
            needs_spot = any((spec.get('spot_15m') or spec.get('spot_1h') for spec in requests.values()))
            spot_symbols: frozenset[str] | None = frozenset() if self.testnet else None
            spot_error = ''
            if needs_spot and (not self.testnet):
                try:
                    spot_symbols = await self._spot_symbols_async()
                except RateLimitDeferred:
                    raise
                except (RuntimeError, TimeoutError, OSError) as exc:
                    spot_error = str(exc)

            async def futures_rows(symbol: str, interval: str, limit: int, end_ms: int) -> list[Candle]:
                async with gate:
                    rows = await self._request_async('/fapi/v1/klines', {'symbol': symbol, 'interval': interval, 'limit': max(1, int(limit)), 'endTime': max(0, int(end_ms) - 1)}, priority='background')
                return [Candle.from_rest(row) for row in rows if len(row) > 7 and int(row[6]) < int(end_ms) and (safe_float(row[1]) > 0) and (safe_float(row[4]) > 0)]

            async def spot_rows(symbol: str, interval: str, limit: int, end_ms: int) -> list[Candle]:
                if self.testnet:
                    return []
                async with gate:
                    rows = await self._spot_klines({'symbol': symbol, 'interval': interval, 'limit': max(1, int(limit)), 'endTime': max(0, int(end_ms) - 1)})
                return [Candle.from_rest(row) for row in rows if len(row) > 7 and int(row[6]) < int(end_ms) and (safe_float(row[1]) > 0) and (safe_float(row[4]) > 0)]

            async def load_symbol(symbol: str, spec: dict[str, Any]) -> tuple[str, dict[str, Any]]:
                calls: list[tuple[str, Any]] = []
                unavailable: list[str] = []
                if spec.get('futures_15m'):
                    calls.append(('futures_15m', futures_rows(symbol, '15m', int(spec.get('futures_15m_limit') or 64), end_15m)))
                if spec.get('futures_1h'):
                    calls.append(('futures_1h', futures_rows(symbol, '1h', int(spec.get('futures_1h_limit') or 96), end_hour)))
                if spec.get('daily'):
                    calls.append(('daily', futures_rows(symbol, '1d', int(spec.get('daily_limit') or 25), day_end)))
                spot_ok = not spot_error and (spot_symbols is None or symbol in spot_symbols)
                if spec.get('spot_15m'):
                    if spot_ok:
                        calls.append(('spot_15m', spot_rows(symbol, '15m', int(spec.get('spot_15m_limit') or 64), end_15m)))
                    elif not spot_error:
                        unavailable.append('spot_15m')
                if spec.get('spot_1h'):
                    if spot_ok:
                        calls.append(('spot_1h', spot_rows(symbol, '1h', int(spec.get('spot_1h_limit') or 96), end_hour)))
                    elif not spot_error:
                        unavailable.append('spot_1h')
                payload: dict[str, Any] = {'_network_calls': len(calls), '_spot_unavailable': tuple(unavailable)}
                values = await asyncio.gather(*(call for _name, call in calls), return_exceptions=True)
                errors: dict[str, str] = {}
                if spot_error:
                    for component in ('spot_15m', 'spot_1h'):
                        if spec.get(component):
                            errors[component] = 'Spot universe unavailable: ' + spot_error
                for (name, _call), value in zip(calls, values):
                    if isinstance(value, RateLimitDeferred):
                        raise value
                    if isinstance(value, BaseException):
                        errors[name] = str(value)
                    elif name not in errors:
                        payload[name] = value
                if errors:
                    payload['errors'] = errors
                return (symbol, payload)
            pairs = await asyncio.gather(*(load_symbol(symbol, spec) for symbol, spec in requests.items()))
            return dict(pairs)
        return run_async(collect())


    def watchlist_hour_changes(self, symbols: list[str]) -> dict[str, float]:
        """Return rolling ~1h last-price changes for a small watchlist."""
        requested = list(dict.fromkeys((str(symbol).upper() for symbol in symbols if symbol)))
        if not requested:
            return {}

        async def fetch(symbol: str) -> tuple[str, float | None]:
            try:
                rows = await self._request_async('/fapi/v1/klines', {'symbol': symbol, 'interval': '1m', 'limit': 61}, priority='background')
            except (RuntimeError, TimeoutError, OSError):
                return (symbol, None)
            if len(rows) < 2:
                return (symbol, None)
            reference = safe_float(rows[0][4])
            current = safe_float(rows[-1][4])
            if reference <= 0 or current <= 0:
                return (symbol, None)
            return (symbol, (current / reference - 1.0) * 100.0)

        async def collect():
            return await asyncio.gather(*(fetch(symbol) for symbol in requested))
        return {symbol: change for symbol, change in run_async(collect()) if change is not None}

    def market_interval_volumes(self, symbols: list[str], hours: int, progress: Callable[[int, int], None]) -> dict[str, dict[str, float]]:
        cutoff = int(time.time() * 1000) - hours * 3600000
        limit = hours * 60 + 2
        requested = list(symbols)
        if not requested:
            return {}

        async def fetch(symbol: str) -> tuple[str, float, float]:
            try:
                rows = await self._request_async('/fapi/v1/klines', {'symbol': symbol, 'interval': '1m', 'limit': limit}, priority='background')
            except (RuntimeError, TimeoutError, OSError):
                return (symbol, 0.0, 0.0)
            recent = [row for row in rows if int(row[0]) >= cutoff]
            return (symbol, sum((safe_float(row[5]) for row in recent)), sum((safe_float(row[7]) for row in recent)))

        async def collect() -> dict[str, dict[str, float]]:
            volumes: dict[str, dict[str, float]] = {}
            tasks = [asyncio.create_task(fetch(symbol)) for symbol in requested]
            completed = 0
            for future in asyncio.as_completed(tasks):
                symbol, base_volume, quote_volume = await future
                volumes[symbol] = {'base': base_volume, 'quote': quote_volume}
                completed += 1
                if completed == len(tasks) or completed % 12 == 0:
                    progress(completed, len(tasks))
            return volumes
        return run_async(collect())

    @staticmethod
    def _rest_candle_matrix(rows: list[list[Any]]) -> np.ndarray:
        """Return [time, open, high, low, close, volume, quote_volume] float64 rows.

        Binance REST already returns a rectangular list-of-lists. NumPy parses
        that block directly in C, avoiding a second Python pass over Candle
        dataclass attributes in the renderer.
        """
        if not rows:
            return np.empty((0, 7), dtype=np.float64)
        raw = np.asarray(rows, dtype=np.float64)
        if raw.ndim != 2 or raw.shape[1] < 8:
            return np.empty((0, 7), dtype=np.float64)
        matrix = np.empty((raw.shape[0], 7), dtype=np.float64, order='C')
        matrix[:, 0] = raw[:, 0] * 0.001
        matrix[:, 1] = raw[:, 1]
        matrix[:, 2] = raw[:, 2]
        matrix[:, 3] = raw[:, 3]
        matrix[:, 4] = raw[:, 4]
        matrix[:, 5] = raw[:, 5]
        matrix[:, 6] = raw[:, 7]
        return matrix

    def candles_snapshot(self, symbol: str, interval: str, *, priority: str='live') -> dict[str, Any]:
        rows = self.get('/fapi/v1/klines', {'symbol': symbol, 'interval': interval, 'limit': 800}, priority=priority)
        return {'symbol': symbol, 'interval': interval, 'candles': [Candle.from_rest(row) for row in rows], 'candle_matrix': self._rest_candle_matrix(rows), '_history_exhausted': len(rows) < 800}

    def older_candles(self, symbol: str, interval: str, before_ms: int, limit: int=1000) -> list[Candle]:
        """Fetch one page ending strictly before an already loaded candle."""
        before_ms = int(before_ms)
        if before_ms <= 0:
            return []
        limit = max(1, min(1000, int(limit)))
        rows = self.get('/fapi/v1/klines', {'symbol': symbol, 'interval': interval, 'endTime': before_ms - 1, 'limit': limit}, priority='background')
        return [Candle.from_rest(row) for row in rows]

    def interest_history(self, symbol: str, interval: str) -> dict[str, Any]:
        rows = self.get('/futures/data/openInterestHist', {'symbol': symbol, 'period': api_period('1d' if interval == '1M' else interval), 'limit': 500}, priority='background')
        return {'symbol': symbol, 'interval': interval, 'oi_history': rows}

    def candle_range(self, symbol: str, interval: str, start_ms: int, end_ms: int, progress: Callable[[int, int], None] | None=None) -> list[Candle]:
        """Fetch a complete range for the persistent historical-data cache."""
        if start_ms <= 0:
            first = self.get('/fapi/v1/klines', {'symbol': symbol, 'interval': interval, 'startTime': 0, 'limit': 1}, priority='background')
            if not first:
                return []
            start_ms = int(first[0][0])
        cursor = start_ms
        candles: list[Candle] = []
        while cursor <= end_ms:
            rows = self.get('/fapi/v1/klines', {'symbol': symbol, 'interval': interval, 'startTime': cursor, 'endTime': end_ms, 'limit': HISTORY_PAGE_LIMIT}, priority='background')
            if not rows:
                break
            candles.extend((Candle.from_rest(row) for row in rows))
            last_open = int(rows[-1][0])
            if progress is not None:
                progress(len(candles), int(min(100, (last_open - start_ms) * 100 / max(1, end_ms - start_ms))))
            next_cursor = int(round(shift_candle_time(last_open / 1000.0, interval) * 1000.0))
            if next_cursor <= cursor or len(rows) < HISTORY_PAGE_LIMIT:
                break
            cursor = next_cursor
        if progress is not None:
            progress(len(candles), 100)
        return candles

    def funding_range(self, symbol: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = max(0, start_ms)
        while cursor <= end_ms:
            page = self.get('/fapi/v1/fundingRate', {'symbol': symbol, 'startTime': cursor, 'endTime': end_ms, 'limit': 1000}, priority='background')
            if not page:
                break
            rows.extend(page)
            last_time = int(page[-1].get('fundingTime', 0))
            next_cursor = last_time + 1
            if next_cursor <= cursor or len(page) < 1000:
                break
            cursor = next_cursor
        return rows


    def download_history(self, symbol: str, interval: str, start_ms: int, end_ms: int, csv_path: str, progress: Callable[[int, int], None], should_cancel: Callable[[], bool], page_callback: Callable[[list[Candle]], None] | None=None) -> dict[str, Any]:
        if start_ms <= 0:
            first = self.get('/fapi/v1/klines', {'symbol': symbol, 'interval': interval, 'startTime': 0, 'limit': 1}, priority='background')
            if not first:
                return {'symbol': symbol, 'interval': interval, 'candles': [], 'total': 0}
            start_ms = int(first[0][0])
        loaded: deque[Candle] = deque(maxlen=MAX_CHART_CANDLES)
        total = 0
        cursor = start_ms
        output = None
        writer = None
        if csv_path:
            os.makedirs(os.path.dirname(os.path.abspath(csv_path)), exist_ok=True)
            output = open(csv_path, 'w', newline='', encoding='utf-8')
            writer = csv.writer(output)
            writer.writerow(('symbol', 'interval', 'open_time', 'open', 'high', 'low', 'close', 'volume', 'quote_volume'))
        try:
            while cursor <= end_ms and (not should_cancel()):
                rows = self.get('/fapi/v1/klines', {'symbol': symbol, 'interval': interval, 'startTime': cursor, 'endTime': end_ms, 'limit': HISTORY_PAGE_LIMIT}, priority='background')
                if not rows:
                    break
                page_candles = [Candle.from_rest(row) for row in rows]
                loaded.extend(page_candles)
                if page_callback is not None:
                    page_callback(page_candles)
                for row in rows:
                    if writer is not None:
                        writer.writerow((symbol, interval, row[0], row[1], row[2], row[3], row[4], row[5], row[7]))
                total += len(rows)
                last_open = int(rows[-1][0])
                percent = int(min(99, (last_open - start_ms) * 100 / max(1, end_ms - start_ms)))
                progress(total, percent)
                next_cursor = int(round(shift_candle_time(last_open / 1000.0, interval) * 1000.0))
                if next_cursor <= cursor or len(rows) < HISTORY_PAGE_LIMIT:
                    break
                cursor = next_cursor
        finally:
            if output is not None:
                output.close()
        progress(total, 100)
        return {'symbol': symbol, 'interval': interval, 'candles': list(loaded), 'total': total, 'csv_path': csv_path, 'cancelled': should_cancel()}

    @staticmethod
    def read_history_csv(symbol: str, interval: str, path: str) -> dict[str, Any]:
        candles: deque[Candle] = deque(maxlen=MAX_CHART_CANDLES)
        total = 0
        with open(path, newline='', encoding='utf-8') as source:
            for row in csv.DictReader(source):
                row_symbol = row.get('symbol') or symbol
                row_interval = row.get('interval') or interval
                if row_symbol != symbol or row_interval != interval:
                    raise ValueError(f'CSV contains {row_symbol} · {row_interval}; current chart is {symbol} · {interval}.')
                candles.append(Candle(time=safe_float(row['open_time']) / 1000.0, open=safe_float(row['open']), high=safe_float(row['high']), low=safe_float(row['low']), close=safe_float(row['close']), volume=safe_float(row['volume']), quote_volume=safe_float(row['quote_volume'])))
                total += 1
        return {'symbol': symbol, 'interval': interval, 'candles': list(candles), 'total': total, 'csv_path': path, 'cancelled': False}

    def metric_history(self, symbol: str, metric: str, period: str, include_price=False) -> dict[str, Any]:
        payload = self._metric_history(symbol, metric, period)
        if include_price:
            if 'price_points' not in payload:
                try:
                    rows = self._request('/fapi/v1/klines', params={'symbol': symbol, 'interval': period, 'limit': 121}, priority='background')
                    now = int(time.time() * 1000)
                    payload['price_points'] = [(r[0], r[4]) for r in rows if len(r) > 6 and float(r[6]) < now][-120:]
                except (RuntimeError, TimeoutError, OSError):
                    payload['price_unavailable'] = True
        else:
            payload.pop('price_points', None)
        return payload

    def _metric_history(self, symbol: str, metric: str, period: str) -> dict[str, Any]:
        """On-demand Data-window histories; all requests use the existing limiter."""
        seconds = {'5m': 300, '1h': 3600, '4h': 14400, '1d': 86400}
        if period not in seconds:
            raise ValueError('Unsupported metric period')
        now = int(time.time() * 1000)
        params = {'symbol': symbol, 'period': period, 'limit': 120}

        def get(path):
            headers = {'X-MBX-APIKEY': self.market_data_api_key} if self.market_data_api_key else None
            return self._request(path, params=params, headers=headers, priority='background')

        def option(name, rows, field, *, details=None):
            return {'name': name, 'series': [(name, [(r['timestamp'], r[field]) for r in rows])], 'details': details or {}}
        if metric == 'volume':
            rows = self._request('/fapi/v1/klines', params={'symbol': symbol, 'interval': period, 'limit': 121}, priority='background')
            points = [(r[0], r[7]) for r in rows if len(r) > 7 and float(r[6]) < now][-120:]
            return {'options': [{'name': 'Volume · USDT', 'series': [('Volume', points)]}], 'bars': True, 'price_points': [(r[0], r[4]) for r in rows if len(r) > 6 and float(r[6]) < now][-120:]}
        if metric == 'taker':
            rows = get('/futures/data/takerlongshortRatio')
            rows = [r for r in rows if float(r['timestamp']) + seconds[period] * 1000 <= now]
            base = symbol.removesuffix('USDT')
            return {'options': [{'name': f'Taker buy / sell · {base}', 'series': [('Buy', [(r['timestamp'], r['buyVol']) for r in rows]), ('Sell', [(r['timestamp'], r['sellVol']) for r in rows])]}], 'bars': True, 'money': False}
        if metric == 'oi':
            return {'options': [option('Open interest · USDT', get('/futures/data/openInterestHist'), 'sumOpenInterestValue')]}
        if metric == 'long_short':
            options = []
            for label, endpoint in (('Top trader accounts', 'topLongShortAccountRatio'), ('Top trader positions', 'topLongShortPositionRatio'), ('All accounts', 'globalLongShortAccountRatio')):
                try:
                    rows = get('/futures/data/' + endpoint)
                    details = {float(r['timestamp']): f"Long {float(r['longAccount']) * 100:.1f}%   Short {float(r['shortAccount']) * 100:.1f}%" for r in rows}
                    options.append(option(label, rows, 'longShortRatio', details=details))
                except (RuntimeError, TimeoutError, OSError):
                    options.append({'name': label, 'series': [], 'empty': 'API key required · Trading settings' if endpoint.startswith('top') and (not self.market_data_api_key) else 'History unavailable · reopen to retry'})
            return {'options': options, 'ratio': True, 'money': False}
        if metric == 'funding':
            start = now - seconds[period] * 1000 * 120
            points = []
            cursor = start
            while cursor <= now:
                rows = self._request('/fapi/v1/fundingRate', params={'symbol': symbol, 'startTime': cursor, 'endTime': now, 'limit': 1000}, priority='background')
                if not rows:
                    break
                points.extend(((r['fundingTime'], float(r['fundingRate']) * 100) for r in rows))
                next_cursor = int(rows[-1]['fundingTime']) + 1
                if len(rows) < 1000 or next_cursor <= cursor:
                    break
                cursor = next_cursor
            return {'options': [{'name': 'Funding · settled %', 'series': [('Funding', points)]}], 'percent': True, 'bars': True, 'time_range': (start, now)}
        raise ValueError('Unknown metric')

    def analysis_snapshot(self, symbol: str, interval: str, current_candles: list[Candle]) -> dict[str, Any]:

        async def optional(path: str, params: dict[str, Any] | None=None, fallback: Any=None) -> Any:
            try:
                if path == '/fapi/v1/klines':
                    return await self._klines_shared(params or {}, 'analytics')
                headers = None
                if path in {'/futures/data/topLongShortAccountRatio', '/futures/data/topLongShortPositionRatio'} and self.market_data_api_key:
                    headers = {'X-MBX-APIKEY': self.market_data_api_key}
                return await self._request_async(path, params=params, headers=headers, priority='analytics')
            except (RuntimeError, TimeoutError, OSError):
                return fallback
        now_ms = int(time.time() * 1000)
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        requests: dict[str, tuple[str, dict[str, Any], Any]] = {'session': ('/fapi/v1/klines', {'symbol': symbol, 'interval': '1m', 'startTime': int(midnight.timestamp() * 1000), 'endTime': now_ms, 'limit': 1500}, []), 'premium': ('/fapi/v1/premiumIndex', {'symbol': symbol}, {}), 'interest': ('/fapi/v1/openInterest', {'symbol': symbol}, {}), 'ticker': ('/fapi/v1/ticker/24hr', {'symbol': symbol}, {}), 'funding_history': ('/fapi/v1/fundingRate', {'symbol': symbol, 'limit': 600}, []), 'interest_detail': ('/futures/data/openInterestHist', {'symbol': symbol, 'period': '5m', 'limit': 49}, []), 'volume_detail': ('/fapi/v1/klines', {'symbol': symbol, 'interval': '1m', 'limit': 242}, []), 'taker_volume': ('/futures/data/takerlongshortRatio', {'symbol': symbol, 'period': '5m', 'limit': 2}, []), 'long_short': ('/futures/data/globalLongShortAccountRatio', {'symbol': symbol, 'period': '5m', 'limit': 30}, []), 'top_long_short_accounts': ('/futures/data/topLongShortAccountRatio', {'symbol': symbol, 'period': '5m', 'limit': 30}, []), 'top_long_short_positions': ('/futures/data/topLongShortPositionRatio', {'symbol': symbol, 'period': '5m', 'limit': 30}, [])}
        for timeframe, limit in (('15m', 600), ('4h', 450), ('1d', 365)):
            if timeframe != interval:
                requests[timeframe] = ('/fapi/v1/klines', {'symbol': symbol, 'interval': timeframe, 'limit': limit}, [])

        async def collect() -> dict[str, Any]:
            names = list(requests)
            values = await asyncio.gather(*(optional(path, params, fallback) for path, params, fallback in requests.values()))
            return dict(zip(names, values))
        results = run_async(collect())
        multi: dict[str, list[Candle]] = {}
        for timeframe in ('15m', '4h', '1d'):
            if timeframe == interval:
                multi[timeframe] = list(current_candles)
            else:
                multi[timeframe] = [Candle.from_rest(row) for row in results.get(timeframe, [])]
        return {'symbol': symbol, 'interval': interval, 'session': [Candle.from_rest(row) for row in results.get('session', [])], 'multi': multi, 'premium': results.get('premium', {}), 'interest': results.get('interest', {}), 'ticker': results.get('ticker', {}), 'funding_history': results.get('funding_history', []), 'interest_detail': results.get('interest_detail', []), 'volume_detail': results.get('volume_detail', []), 'long_short': results.get('long_short', []), 'top_long_short_accounts': results.get('top_long_short_accounts', []), 'top_long_short_positions': results.get('top_long_short_positions', []), 'taker_volume': results.get('taker_volume', []), 'top_trader_requires_key': not bool(self.market_data_api_key)}

    def current_interest(self, symbol: str) -> dict[str, Any]:
        return self.get('/fapi/v1/openInterest', {'symbol': symbol})

    def place_order(self, api_key: str, api_secret: str, order: dict[str, Any], *, before_send: Callable[[], None] | None = None) -> dict[str, Any]:
        """Place exactly one order; conditional orders use Binance's Algo service."""
        params = dict(order)
        order_type = str(params.get('type', '')).upper()
        if order_type in CONDITIONAL_ORDER_TYPES:
            params.setdefault('algoType', 'CONDITIONAL')
            params.setdefault('clientAlgoId', params.pop('newClientOrderId', ''))
            return self.signed_request(api_key, api_secret, '/fapi/v1/algoOrder', params, 'POST', order_count=1, before_send=before_send)
        params.setdefault('newOrderRespType', 'RESULT')
        return self.signed_request(api_key, api_secret, '/fapi/v1/order', params, 'POST', order_count=1, before_send=before_send)

    def place_batch_orders(self, api_key: str, api_secret: str, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not orders:
            return []
        if any((str(order.get('type', '')).upper() in CONDITIONAL_ORDER_TYPES for order in orders)):
            results: list[dict[str, Any]] = []
            for index, order in enumerate(orders):
                try:
                    results.append(self.place_order(api_key, api_secret, order))
                except (RuntimeError, TimeoutError, OSError) as exc:
                    results.append({'code': binance_error_code(exc) or -1, 'msg': str(exc), '_uncertain': execution_outcome_uncertain(exc)})
                    results.extend(({'code': -1, 'msg': 'Not sent after an earlier conditional-order failure.', '_notSent': True} for _order in orders[index + 1:]))
                    break
            return results
        encoded_orders = [self._encoded_params(order) for order in orders]
        return self.signed_request(api_key, api_secret, '/fapi/v1/batchOrders', {'batchOrders': encoded_orders}, 'POST', order_count=5)

    def modify_order(self, api_key: str, api_secret: str, changes: dict[str, Any]) -> dict[str, Any]:
        return self.signed_request(api_key, api_secret, '/fapi/v1/order', changes, 'PUT', order_count=1)

    def cancel_order(self, api_key: str, api_secret: str, request: dict[str, Any], algo: bool=False) -> dict[str, Any]:
        params = {key: value for key, value in request.items() if key in {'algoId', 'clientAlgoId'}} if algo else dict(request)
        return self.signed_request(api_key, api_secret, '/fapi/v1/algoOrder' if algo else '/fapi/v1/order', params, 'DELETE')

    def cancel_all_orders(self, api_key: str, api_secret: str, symbol: str) -> dict[str, Any]:
        try:
            standard = self.signed_request(api_key, api_secret, '/fapi/v1/allOpenOrders', {'symbol': symbol}, 'DELETE')
        except (RuntimeError, TimeoutError, OSError) as exc:
            standard = {'error': str(exc), 'uncertain': execution_outcome_uncertain(exc)}
        try:
            algo = self.signed_request(api_key, api_secret, '/fapi/v1/algoOpenOrders', {'symbol': symbol}, 'DELETE')
        except (RuntimeError, TimeoutError, OSError) as exc:
            algo = {'error': str(exc), 'uncertain': execution_outcome_uncertain(exc)}
        return {'standard': standard, 'algo': algo}

    def query_order_by_client_id(self, api_key: str, api_secret: str, symbol: str, client_id: str, algo: bool=False) -> dict[str, Any]:
        parameter = 'clientAlgoId' if algo else 'origClientOrderId'
        params = {parameter: client_id}
        if not algo:
            params['symbol'] = symbol
        return self.query_order_request(api_key, api_secret, params, algo=algo)

    def query_order(self, api_key: str, api_secret: str, symbol: str, order_id: Any) -> dict[str, Any]:
        return self.query_order_request(api_key, api_secret, {'symbol': symbol, 'orderId': order_id})

    def query_order_request(self, api_key: str, api_secret: str, request: dict[str, Any], algo: bool=False) -> dict[str, Any]:
        keys = {'algoId', 'clientAlgoId'} if algo else {'symbol', 'orderId', 'origClientOrderId'}
        params = {key: value for key, value in request.items() if key in keys}
        result = self.signed_request(api_key, api_secret, '/fapi/v1/algoOrder' if algo else '/fapi/v1/order', params, 'GET')
        id_key, client_key = ('algoId', 'clientAlgoId') if algo else ('orderId', 'clientOrderId')
        wanted_client = params.get('clientAlgoId' if algo else 'origClientOrderId')
        if (not isinstance(result, dict) or safe_float(result.get(id_key)) <= 0
                or (params.get(id_key) is not None and str(result.get(id_key)) != str(params[id_key]))
                or (wanted_client and result.get(client_key) != wanted_client)
                or (params.get('symbol') and result.get('symbol') != params['symbol'])):
            raise RuntimeError('Unexpected response: queried Binance order identity could not be verified.')
        return result

    def position_mode(self, api_key: str, api_secret: str, force: bool=False) -> dict[str, Any]:
        return run_async(self._position_mode_async(api_key, api_secret, force))

    async def _position_mode_async(self, api_key: str, api_secret: str, force: bool=False, *, priority: str='manual') -> dict[str, Any]:
        if self._position_mode_async_lock is None:
            self._position_mode_async_lock = asyncio.Lock()
        async with self._position_mode_async_lock:
            with self._position_mode_lock:
                cached = self._position_mode_cache
            if not force and cached is not None and cached[0] == api_key and time.monotonic() - cached[1] < 60.0:
                return dict(cached[2])
            payload = await self._signed_request_async(api_key, api_secret, '/fapi/v1/positionSide/dual', priority=priority)
            result = dict(payload) if isinstance(payload, dict) else {}
            if 'dualSidePosition' not in result:
                raise RuntimeError('Binance omitted dualSidePosition from the position-mode response.')
            with self._position_mode_lock:
                self._position_mode_cache = (api_key, time.monotonic(), result)
            return dict(result)

    def invalidate_position_mode_cache(self) -> None:
        with self._position_mode_lock:
            self._position_mode_cache = None

    def change_leverage(self, api_key: str, api_secret: str, symbol: str, leverage: int) -> dict[str, Any]:
        return self.signed_request(api_key, api_secret, '/fapi/v1/leverage', {'symbol': symbol, 'leverage': max(1, min(125, int(leverage)))}, 'POST')

    def ensure_cross_margin(self, api_key: str, api_secret: str, symbol: str) -> dict[str, Any]:
        try:
            return self.signed_request(api_key, api_secret, '/fapi/v1/marginType', {'symbol': symbol, 'marginType': 'CROSSED'}, 'POST')
        except RuntimeError as exc:
            if 'No need to change margin type' in str(exc) or '-4046' in str(exc):
                return {'symbol': symbol, 'marginType': 'CROSSED', 'unchanged': True}
            raise

    def account_snapshot(self, api_key: str, api_secret: str, symbol: str | None=None, all_open_orders: bool=False) -> dict[str, Any]:
        symbol_param = {'symbol': symbol} if symbol else {}
        order_param = {} if all_open_orders else symbol_param
        async def collect():
            async def read(path, params=None, fallback=None):
                try:
                    return await self._signed_request_async(api_key, api_secret, path, params, priority='reconciliation')
                except RuntimeError as exc:
                    if fallback and any(token in str(exc) for token in ('404', 'Unknown endpoint')):
                        return await self._signed_request_async(api_key, api_secret, fallback, params, priority='reconciliation')
                    raise

            async def fills_read():
                return await read('/fapi/v1/userTrades', {'symbol': symbol, 'limit': 100}) if symbol else []

            values = await asyncio.gather(
                read('/fapi/v3/account', fallback='/fapi/v2/account'),
                read('/fapi/v3/positionRisk', fallback='/fapi/v2/positionRisk'),
                read('/fapi/v1/openOrders', order_param), fills_read(),
                read('/fapi/v1/openAlgoOrders', order_param),
                read('/fapi/v1/accountConfig'),
                read('/fapi/v1/symbolConfig'),
                return_exceptions=True,
            )
            for value in values:
                if isinstance(value, BaseException):
                    raise value
            return values

        account, position_risk, open_orders, fills, algo_orders, account_config, symbol_config = run_async(collect())
        if not isinstance(account_config, dict) or not isinstance(symbol_config, list):
            raise RuntimeError('Unexpected Binance account/symbol configuration response.')
        configs = {str(row.get('symbol')): row for row in symbol_config if isinstance(row, dict)}
        position_mode = ({'dualSidePosition': account_config['dualSidePosition']}
                         if 'dualSidePosition' in account_config
                         else run_async(self._position_mode_async(api_key, api_secret, priority='reconciliation')))
        if not isinstance(account, dict):
            raise RuntimeError('Unexpected response: Binance account payload is not an object.')
        if not isinstance(position_risk, list):
            raise RuntimeError('Unexpected response: Binance position-risk payload is not a list.')
        if isinstance(account, dict):
            account = dict(account)
            account.update({key: account_config[key] for key in ('canTrade', 'multiAssetsMargin') if key in account_config})
            account_positions = [dict(row) for row in account.get('positions', []) if isinstance(row, dict)]
            account_keys = {(str(row.get('symbol', '')), str(row.get('positionSide', 'BOTH'))) for row in account_positions}
            risk_by_key = {(str(row.get('symbol', '')), str(row.get('positionSide', 'BOTH'))): row for row in position_risk if isinstance(row, dict) and row.get('symbol')}
            merged_positions: list[dict[str, Any]] = []
            for row in account_positions:
                key = (str(row.get('symbol', '')), str(row.get('positionSide', 'BOTH')))
                merged = {**row, **risk_by_key.get(key, {}), **configs.get(key[0], {})}
                if 'unRealizedProfit' in merged:
                    merged['unrealizedProfit'] = merged['unRealizedProfit']
                merged_positions.append(merged)
            for key, row in risk_by_key.items():
                if key in account_keys or abs(safe_float(row.get('positionAmt'))) <= 0:
                    continue
                merged = {**row, **configs.get(key[0], {})}
                if 'unRealizedProfit' in merged:
                    merged['unrealizedProfit'] = merged['unRealizedProfit']
                merged_positions.append(merged)
            account['positions'] = merged_positions
        if not isinstance(open_orders, list):
            raise RuntimeError('Unexpected response: Binance open-orders payload is not a list.')
        if not isinstance(fills, list):
            raise RuntimeError('Unexpected response: Binance trade-history payload is not a list.')
        if isinstance(algo_orders, dict):
            algo_orders = next((algo_orders[key] for key in ('orders', 'rows', 'data') if isinstance(algo_orders.get(key), list)), None)
        if not isinstance(algo_orders, list):
            raise RuntimeError('Unexpected response: Binance open-Algo payload is not a list.')
        return {'account': account, 'accountConfig': account_config, 'symbolConfig': symbol_config, 'positionRisk': position_risk, 'orders': open_orders, 'algoOrders': algo_orders, 'ordersScope': 'ALL' if all_open_orders or not symbol else symbol, 'fills': fills, 'fillsSymbol': symbol or '', 'positionMode': position_mode}

    def start_user_stream(self, api_key: str) -> str:
        payload = self._request('/fapi/v1/listenKey', method='POST', headers={'X-MBX-APIKEY': api_key}, priority='live')
        return str(payload.get('listenKey', ''))

    def keepalive_user_stream(self, api_key: str, listen_key: str) -> dict[str, Any]:
        del listen_key
        return self._request('/fapi/v1/listenKey', method='PUT', headers={'X-MBX-APIKEY': api_key}, priority='live')
