"""Public feed parsing, validated depth lifecycle and market-data orchestration."""
from __future__ import annotations




import json
import math
from bisect import bisect_left, bisect_right, insort
import time
import threading
from collections import deque
from typing import Any

from PySide6 import QtCore
from PySide6.QtCore import QTimer, Signal

from ..models import safe_float
from ..models import DiagnosticsPort, book_data_is_fresh

def _strict_float(value: Any) -> float | None:
    """Return a finite float without coercing malformed data to zero."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _strict_int(value: Any) -> int | None:
    """Return only genuine integer JSON values or canonical integer strings.

    Exchange sequence identifiers are integers.  Do not silently accept booleans
    (``bool`` is an ``int`` subclass in Python), truncate floats, or parse scientific
    notation/decimal strings into a different identifier.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    digits = text[1:] if text[:1] in {"+", "-"} else text
    if not digits or not digits.isascii() or not digits.isdigit():
        return None
    try:
        return int(text, 10)
    except (TypeError, ValueError, OverflowError):
        return None


def _clean_depth_rows(rows: Any, *, reverse: bool) -> list[tuple[float, float]]:
    cleaned: list[tuple[float, float]] = []
    if not isinstance(rows, list):
        return cleaned
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        price = safe_float(row[0])
        quantity = safe_float(row[1])
        if price > 0.0 and quantity > 0.0 and math.isfinite(price * quantity):
            cleaned.append((price, quantity))
    cleaned.sort(key=lambda value: value[0], reverse=reverse)
    return cleaned[:20]


def _enrich_agg_trade(event: dict[str, Any]) -> dict[str, Any]:
    """Attach RPI-aware quantities without converting malformed ``nq`` into RPI flow."""
    total_raw = _strict_float(event.get("q"))
    total_quantity = total_raw if total_raw is not None and total_raw >= 0.0 else 0.0

    normal_raw = _strict_float(event.get("nq")) if "nq" in event else None
    rpi_known = (
        normal_raw is not None
        and 0.0 <= normal_raw <= total_quantity
    )
    normal_quantity = normal_raw if rpi_known else total_quantity
    rpi_quantity = total_quantity - normal_quantity if rpi_known else 0.0

    price_raw = _strict_float(event.get("p"))
    price = price_raw if price_raw is not None and price_raw > 0.0 else 0.0

    enriched = dict(event)
    enriched["_rpi_known"] = bool(rpi_known)
    enriched["_normal_quantity"] = float(normal_quantity)
    enriched["_rpi_quantity"] = float(max(0.0, rpi_quantity))
    enriched["_rpi_share"] = (
        rpi_quantity / total_quantity if rpi_known and total_quantity > 0.0 else 0.0
    )
    enriched["_normal_notional"] = price * normal_quantity
    enriched["_rpi_notional"] = price * max(0.0, rpi_quantity)
    return enriched


def _decode_socket_payload(
    kind: str,
    message: str,
    symbol: str,
    interval: str,
    ticker_symbols: tuple[str, ...],
    valid_ticker_symbols: frozenset[str] = frozenset(),
) -> tuple[tuple[str, Any], ...]:
    """Parse and classify a websocket frame outside the GUI thread."""
    try:
        wrapper = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return ()

    stream_name = str(wrapper.get("stream", "")) if isinstance(wrapper, dict) else ""
    data = wrapper.get("data", wrapper) if isinstance(wrapper, dict) else wrapper
    packets: list[tuple[str, Any]] = []

    if isinstance(data, list):
        tickers: list[dict[str, Any]] = []
        marks: list[dict[str, Any]] = []
        trades: list[dict[str, Any]] = []
        liquidations: list[dict[str, Any]] = []
        tracked = set(ticker_symbols)
        valid_tickers = valid_ticker_symbols

        for item in data:
            if not isinstance(item, dict) or item.get("st", 1) != 1:
                continue
            event = item.get("e")
            if event in {"24hrTicker", "24hrMiniTicker"}:
                item_symbol = str(item.get("s") or "")
                if valid_tickers and item_symbol not in valid_tickers:
                    continue
                if event == "24hrMiniTicker" and "P" not in item:
                    opened = safe_float(item.get("o"))
                    closed = safe_float(item.get("c"))
                    item = dict(item)
                    item["P"] = ((closed - opened) / opened * 100.0) if opened > 0.0 else 0.0
                tickers.append(item)
            elif event == "markPriceUpdate":
                item_symbol = str(item.get("s") or "")
                if item_symbol in tracked and item_symbol != symbol:
                    marks.append(item)
            elif event == "aggTrade":
                trades.append(_enrich_agg_trade(item))
            elif event == "forceOrder":
                liquidations.append(item)

        if tickers:
            packets.append(("tickers", tickers))
        if marks:
            packets.append(("marks", marks))
        if trades:
            packets.append(("trades", trades))
        if liquidations:
            packets.append(("liquidations", liquidations))
        return tuple(packets)

    if not isinstance(data, dict) or data.get("st", 1) != 1:
        return ()

    event = data.get("e")



    if event == "bookTicker" or stream_name.lower().endswith("@bookticker"):
        item_symbol = str(data.get("s") or stream_name.partition("@")[0]).upper()
        if item_symbol != str(symbol).upper():
            return ()
        payload = dict(data)
        payload["s"] = item_symbol
        return (("book_ticker", payload),)




    if kind == "public" and (
        event == "depthUpdate" or stream_name.lower().find("@depth") >= 0
    ):
        return ()

    if event == "kline":
        row = data.get("k") or {}
        if data.get("s") == symbol and row.get("i") == interval:
            return (("kline", data),)
        return ()
    if event == "markPriceUpdate":
        return (("mark", data),)
    if event == "forceOrder":
        return (("liquidation", data),)
    if event == "aggTrade":
        return (("trade", _enrich_agg_trade(data)),)
    if event in {"24hrTicker", "24hrMiniTicker"}:
        if event == "24hrMiniTicker" and "P" not in data:
            opened = safe_float(data.get("o"))
            closed = safe_float(data.get("c"))
            data = dict(data)
            data["P"] = ((closed - opened) / opened * 100.0) if opened > 0.0 else 0.0
        return (("tickers", [data]),)
    return ()


class _SocketParserWorker(QtCore.QObject):
    """Parse non-depth websocket frames behind one bounded worker wakeup.

    Raw websocket delivery must be bounded *before* Qt's queued worker-event
    stream.  The GUI/socket thread therefore appends to a lock-protected ingress
    deque and schedules at most one ``drain()`` invocation.  If that queue
    overflows or ages beyond the safety boundary, old work is discarded and the
    owning transport leg is told to reconnect rather than replay stale trades
    into current order-flow analytics.
    """

    parsed = Signal(str, int, object)
    backpressure = Signal(str, int, str)

    MAX_INGRESS_EVENTS = 512
    MAX_PARSER_QUEUE_AGE_MS = 1_500.0
    MAX_TRADE_BUFFER_EVENTS = 1_024

    def __init__(self) -> None:
        super().__init__()
        self._trade_buffer: deque[tuple[str, int, dict[str, Any]]] = deque()
        self._trade_timer = QTimer(self)
        self._trade_timer.setSingleShot(True)
        self._trade_timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self._trade_timer.setInterval(16)
        self._trade_timer.timeout.connect(self._flush_trades)



        self._book_ticker_pending: tuple[str, int, dict[str, Any]] | None = None
        self._book_ticker_timer = QTimer(self)
        self._book_ticker_timer.setSingleShot(True)
        self._book_ticker_timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self._book_ticker_timer.setInterval(16)
        self._book_ticker_timer.timeout.connect(self._flush_book_ticker)



        self._ingress_lock = threading.Lock()
        self._ingress: deque[
            tuple[str, int, str, str, str, object, frozenset[str], float]
        ] = deque()
        self._ingress_scheduled = False
        self._overflow_contexts: set[tuple[str, int]] = set()

    def enqueue(
        self,
        kind: str,
        generation: int,
        message: str,
        symbol: str,
        interval: str,
        ticker_symbols: object,
        valid_ticker_symbols: object,
        arrival_mono_ms: float,
    ) -> bool:
        """Thread-safe bounded raw ingress.

        Returns ``True`` only when the caller must queue one worker ``drain``
        wakeup.  On overflow, already-stale raw work is dropped wholesale.  The
        worker later emits ``backpressure`` for every affected transport token so
        the hub can establish a clean inference boundary.
        """
        valid_snapshot = (
            valid_ticker_symbols
            if isinstance(valid_ticker_symbols, frozenset)
            else frozenset(str(value) for value in (valid_ticker_symbols or ()))
        )
        item = (
            str(kind),
            int(generation),
            str(message),
            str(symbol),
            str(interval),
            tuple(ticker_symbols or ()),
            valid_snapshot,
            float(arrival_mono_ms or 0.0),
        )
        with self._ingress_lock:
            if len(self._ingress) >= self.MAX_INGRESS_EVENTS:
                self._overflow_contexts.update(
                    (queued[0], queued[1]) for queued in self._ingress
                )
                self._overflow_contexts.add((item[0], item[1]))
                self._ingress.clear()
            self._ingress.append(item)
            if self._ingress_scheduled:
                return False
            self._ingress_scheduled = True
            return True

    def _discard_coalesced_state(self) -> None:
        """Drop data derived from a parser interval known to contain a gap."""
        self._trade_buffer.clear()
        self._book_ticker_pending = None
        if self._trade_timer.isActive():
            self._trade_timer.stop()
        if self._book_ticker_timer.isActive():
            self._book_ticker_timer.stop()

    @QtCore.Slot()
    def drain(self) -> None:
        """Drain a bounded batch and reject work that waited too long to parse."""
        while True:
            with self._ingress_lock:
                if not self._ingress:
                    self._ingress_scheduled = False
                    return
                batch = list(self._ingress)
                self._ingress.clear()
                affected = set(self._overflow_contexts)
                self._overflow_contexts.clear()

            now_ms = time.perf_counter() * 1000.0
            fresh: list[tuple[str, int, str, str, str, object, frozenset[str], float]] = []
            for item in batch:
                arrival_mono_ms = item[7]
                if (
                    arrival_mono_ms > 0.0
                    and now_ms - arrival_mono_ms > self.MAX_PARSER_QUEUE_AGE_MS
                ):
                    affected.add((item[0], item[1]))
                    continue
                fresh.append(item)

            if affected:



                self._discard_coalesced_state()
                fresh = [
                    item for item in fresh
                    if (item[0], item[1]) not in affected
                ]
                for kind, generation in sorted(affected):
                    self.backpressure.emit(
                        kind,
                        generation,
                        "SOCKET PARSER BACKPRESSURE",
                    )

            for item in fresh:
                self.parse(*item[:7])

    def _signal_trade_backpressure(self, kind: str, generation: int) -> None:
        self._discard_coalesced_state()
        self.backpressure.emit(
            str(kind), int(generation), "TRADE COALESCER OVERFLOW"
        )

    def parse(
        self,
        kind: str,
        generation: int,
        message: str,
        symbol: str,
        interval: str,
        ticker_symbols: object,
        valid_ticker_symbols: object,
    ) -> None:
        valid_snapshot = (
            valid_ticker_symbols
            if isinstance(valid_ticker_symbols, frozenset)
            else frozenset(str(value) for value in (valid_ticker_symbols or ()))
        )
        packets = _decode_socket_payload(
            kind,
            message,
            symbol,
            interval,
            tuple(ticker_symbols or ()),
            valid_snapshot,
        )
        if not packets:
            return

        immediate: list[tuple[str, Any]] = []
        for packet_type, payload in packets:
            if packet_type == "trade":
                if isinstance(payload, dict):
                    if len(self._trade_buffer) >= self.MAX_TRADE_BUFFER_EVENTS:
                        self._signal_trade_backpressure(kind, generation)
                        return
                    self._trade_buffer.append((kind, generation, payload))
                continue
            if packet_type == "trades":
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    if len(self._trade_buffer) >= self.MAX_TRADE_BUFFER_EVENTS:
                        self._signal_trade_backpressure(kind, generation)
                        return
                    self._trade_buffer.append((kind, generation, item))
                continue
            if packet_type == "book_ticker":
                if isinstance(payload, dict):
                    self._book_ticker_pending = (kind, generation, payload)
                continue
            immediate.append((packet_type, payload))

        if immediate:
            self.parsed.emit(kind, generation, tuple(immediate))

        if self._trade_buffer and not self._trade_timer.isActive():
            self._trade_timer.start()
        if (
            self._book_ticker_pending is not None
            and not self._book_ticker_timer.isActive()
        ):
            self._book_ticker_timer.start()

    @QtCore.Slot()
    def _flush_book_ticker(self) -> None:
        pending = self._book_ticker_pending
        self._book_ticker_pending = None
        if pending is None:
            return
        kind, generation, payload = pending
        self.parsed.emit(
            kind,
            generation,
            (("book_ticker", payload),),
        )

    @QtCore.Slot()
    def _flush_trades(self) -> None:
        if not self._trade_buffer:
            return



        grouped: list[tuple[str, int, list[dict[str, Any]]]] = []
        current_kind = ""
        current_generation = -1
        current_batch: list[dict[str, Any]] = []

        while self._trade_buffer:
            kind, generation, payload = self._trade_buffer.popleft()
            if (
                current_batch
                and (kind != current_kind or generation != current_generation)
            ):
                grouped.append(
                    (current_kind, current_generation, current_batch)
                )
                current_batch = []
            current_kind = kind
            current_generation = generation
            current_batch.append(payload)

        if current_batch:
            grouped.append((current_kind, current_generation, current_batch))

        for kind, generation, batch in grouped:
            self.parsed.emit(
                kind,
                generation,
                (("trade_batch", batch),),
            )


class _DepthParserWorker(QtCore.QObject):
    """Maintain one validated, synchronized USD-M local book off the GUI thread."""

    parsed = Signal(int, int, object)
    resync_required = Signal(int, int)
    invalidated = Signal(int, int, str)

    MAX_BUFFERED_EVENTS = 512
    MAX_INGRESS_EVENTS = 512




    PUBLISH_LEVELS = 120
    ANALYSIS_LEVELS = 1000
    DEPTH_NOTIONAL_LEVELS = 1000
    MAX_PARSER_QUEUE_AGE_MS = 1_500.0
    COVERAGE_MONITOR_MIN_LEVELS = 900
    COVERAGE_RESERVE_LEVELS = 256
    MAX_BOOK_LEVELS_BEFORE_REBASE = 4_096

    def __init__(self) -> None:
        super().__init__()
        self._epoch = -1
        self._revision = 0
        self._symbol = ""
        self._snapshot_update_id = 0
        self._last_update_id = 0
        self._seeded = False
        self._ready = False
        self._resync_pending = False
        self._bids: dict[float, float] = {}
        self._asks: dict[float, float] = {}


        self._bid_prices: list[float] = []
        self._ask_prices: list[float] = []
        self._analysis_publish_levels = self.PUBLISH_LEVELS
        self._published_analysis_bids: list[tuple[float, float]] = []
        self._published_analysis_asks: list[tuple[float, float]] = []
        self._analysis_revision = 0
        self._analysis_window_dirty = True
        self._depth_bid_notional = 0.0
        self._depth_ask_notional = 0.0
        self._snapshot_bid_prices: tuple[float, ...] = ()
        self._snapshot_ask_prices: tuple[float, ...] = ()
        self._monitor_bid_coverage = False
        self._monitor_ask_coverage = False
        self._buffer: deque[dict[str, Any]] = deque()


        self._ingress_lock = threading.Lock()
        self._ingress: deque[tuple[int, str, str, object]] = deque()
        self._ingress_scheduled = False
        self._ingress_overflow = False

    def enqueue(
        self,
        epoch: int,
        message: str,
        symbol: str,
        timing: object,
    ) -> bool:
        """Thread-safe bounded ingress; return True when a drain wakeup is needed."""
        with self._ingress_lock:
            if len(self._ingress) >= self.MAX_INGRESS_EVENTS:
                self._ingress.clear()
                self._ingress_overflow = True
            self._ingress.append((int(epoch), str(message), str(symbol), timing))
            if self._ingress_scheduled:
                return False
            self._ingress_scheduled = True
            return True

    @QtCore.Slot()
    def drain(self) -> None:
        """Drain all currently queued depth frames on the worker thread."""
        while True:
            with self._ingress_lock:
                if not self._ingress:
                    self._ingress_scheduled = False
                    return
                batch = list(self._ingress)
                self._ingress.clear()
                overflow = self._ingress_overflow
                self._ingress_overflow = False
            if overflow:
                self._request_resync("DEPTH INGRESS OVERFLOW", force=True)
            for epoch, message, symbol, timing in batch:
                self.parse(epoch, message, symbol, timing)

    def _reset_state(
        self,
        epoch: int,
        revision: int,
        symbol: str = "",
    ) -> None:
        self._epoch = int(epoch)
        self._revision = max(0, int(revision))
        self._symbol = str(symbol or "").upper()
        self._snapshot_update_id = 0
        self._last_update_id = 0
        self._seeded = False
        self._ready = False
        self._resync_pending = False
        self._bids.clear()
        self._asks.clear()
        self._bid_prices.clear()
        self._ask_prices.clear()
        self._published_analysis_bids = []
        self._published_analysis_asks = []
        self._analysis_revision += 1
        self._analysis_window_dirty = True
        self._depth_bid_notional = 0.0
        self._depth_ask_notional = 0.0
        self._snapshot_bid_prices = ()
        self._snapshot_ask_prices = ()
        self._monitor_bid_coverage = False
        self._monitor_ask_coverage = False
        self._buffer.clear()

    @QtCore.Slot(int, int, str)
    def reset_epoch(self, epoch: int, revision: int, symbol: str) -> None:
        epoch = int(epoch)
        revision = int(revision)
        if epoch < self._epoch:
            return
        if epoch == self._epoch and revision < self._revision:
            return
        self._reset_state(epoch, revision, symbol)

    @staticmethod
    def _snapshot_side(rows: Any) -> dict[float, float] | None:
        if not isinstance(rows, list) or not rows:
            return None
        output: dict[float, float] = {}
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                return None
            price = _strict_float(row[0])
            quantity = _strict_float(row[1])
            if (
                price is None or quantity is None
                or price <= 0.0 or quantity <= 0.0
            ):
                return None
            output[price] = quantity
        return output or None

    @staticmethod
    def _validated_delta_side(rows: Any) -> list[tuple[float, float]] | None:
        if not isinstance(rows, list):
            return None
        output: list[tuple[float, float]] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                return None
            price = _strict_float(row[0])
            quantity = _strict_float(row[1])
            if (
                price is None or quantity is None
                or price <= 0.0 or quantity < 0.0
            ):
                return None
            output.append((price, quantity))
        return output

    @staticmethod
    def _apply_side(
        book: dict[float, float],
        prices: list[float],
        rows: Any,
    ) -> tuple[bool, list[tuple[float, float, float]]]:
        """Apply one validated side and report topology/quantity changes."""
        topology_changed = False
        changes: list[tuple[float, float, float]] = []
        for price, quantity in rows:
            previous = book.get(price, 0.0)
            existed = price in book
            if quantity == 0.0:
                if not existed:
                    continue
                book.pop(price, None)
                index = bisect_left(prices, price)
                if index < len(prices) and prices[index] == price:
                    prices.pop(index)
                topology_changed = True
                changes.append((price, previous, 0.0))
                continue
            if existed and quantity == previous:
                continue
            if not existed:
                insort(prices, price)
                topology_changed = True
            book[price] = quantity
            changes.append((price, previous, quantity))
        return topology_changed, changes

    def _depth_notional_for_side(self, side: str) -> float:
        if side == 'bid':
            prices = self._bid_prices
            book = self._bids
            start = max(0, len(prices) - self.DEPTH_NOTIONAL_LEVELS)
            return sum(price * book[price] for price in prices[start:])
        prices = self._ask_prices
        book = self._asks
        stop = min(len(prices), self.DEPTH_NOTIONAL_LEVELS)
        return sum(price * book[price] for price in prices[:stop])

    def _update_depth_notional(
        self,
        side: str,
        topology_changed: bool,
        changes: list[tuple[float, float, float]],
    ) -> None:
        if not changes:
            return
        if topology_changed:
            total = self._depth_notional_for_side(side)
        else:
            prices = self._bid_prices if side == 'bid' else self._ask_prices
            if not prices:
                total = 0.0
            else:
                count = min(len(prices), self.DEPTH_NOTIONAL_LEVELS)
                boundary = prices[-count] if side == 'bid' else prices[count - 1]
                total = self._depth_bid_notional if side == 'bid' else self._depth_ask_notional
                for price, old_quantity, new_quantity in changes:
                    inside = price >= boundary if side == 'bid' else price <= boundary
                    if inside:
                        total += price * (new_quantity - old_quantity)
        if side == 'bid':
            self._depth_bid_notional = max(0.0, total)
        else:
            self._depth_ask_notional = max(0.0, total)

    def _changes_touch_analysis_window(
        self,
        side: str,
        topology_changed: bool,
        changes: list[tuple[float, float, float]],
    ) -> bool:
        if topology_changed:
            return True
        if not changes:
            return False
        prices = self._bid_prices if side == 'bid' else self._ask_prices
        if not prices:
            return True
        count = min(
            len(prices),
            max(self.PUBLISH_LEVELS, min(self._analysis_publish_levels, self.ANALYSIS_LEVELS)),
        )
        if count <= 0:
            return True
        boundary = prices[-count] if side == 'bid' else prices[count - 1]
        return any(
            (price >= boundary if side == 'bid' else price <= boundary)
            for price, _old_quantity, _new_quantity in changes
        )

    @QtCore.Slot(int)
    def set_analysis_publish_levels(self, limit_per_side: int) -> None:
        try:
            resolved = int(limit_per_side)
        except (TypeError, ValueError, OverflowError):
            resolved = self.PUBLISH_LEVELS
        resolved = max(self.PUBLISH_LEVELS, min(resolved, self.ANALYSIS_LEVELS))
        if resolved == self._analysis_publish_levels:
            return
        self._analysis_publish_levels = resolved

        self._published_analysis_bids = []
        self._published_analysis_asks = []
        self._analysis_window_dirty = True

    @staticmethod
    def _decode_delta(
        message: str,
        fallback_symbol: str,
        timing: object,
    ) -> dict[str, Any] | None:
        try:
            wrapper = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return None
        stream_name = (
            str(wrapper.get("stream", ""))
            if isinstance(wrapper, dict)
            else ""
        )
        data = wrapper.get("data", wrapper) if isinstance(wrapper, dict) else wrapper
        if not isinstance(data, dict) or data.get("e") != "depthUpdate":
            return None

        stream_type = _strict_int(data.get("st", 1))
        if stream_type != 1:
            return None

        expected_symbol = str(fallback_symbol or "").upper()
        stream_symbol = stream_name.partition("@")[0].strip()
        explicit_symbol = data.get("s") or stream_symbol



        if not explicit_symbol:
            return None
        event_symbol = str(explicit_symbol).upper()
        if not expected_symbol or event_symbol != expected_symbol:
            return None

        first_id = _strict_int(data.get("U"))
        final_id = _strict_int(data.get("u"))
        previous_id = _strict_int(data.get("pu"))
        if (
            first_id is None or final_id is None or previous_id is None
            or first_id <= 0 or final_id < first_id or previous_id < 0
        ):
            return None

        bids = _DepthParserWorker._validated_delta_side(data.get("b"))
        asks = _DepthParserWorker._validated_delta_side(data.get("a"))
        if bids is None or asks is None:
            return None

        arrival_wall_ms = 0.0
        arrival_mono_ms = 0.0
        server_arrival_ms = 0.0
        enqueue_mono_ms = 0.0
        if isinstance(timing, (tuple, list)):
            if len(timing) >= 1:
                arrival_wall_ms = safe_float(timing[0])
            if len(timing) >= 2:
                arrival_mono_ms = safe_float(timing[1])
            if len(timing) >= 3:
                server_arrival_ms = safe_float(timing[2])
            if len(timing) >= 4:
                enqueue_mono_ms = safe_float(timing[3])

        return {
            "s": event_symbol,
            "E": data.get("E", 0),
            "T": data.get("T", 0),
            "U": first_id,
            "u": final_id,
            "pu": previous_id,
            "b": bids,
            "a": asks,
            "_socket_received_wall_ms": float(arrival_wall_ms or 0.0),
            "_socket_received_mono_ms": float(arrival_mono_ms or 0.0),
            "_server_received_ms": float(server_arrival_ms or 0.0),
            "_enqueue_mono_ms": float(enqueue_mono_ms or 0.0),
        }


    def _signal_invalid(self, reason: str, *, force: bool = False) -> None:
        reason = str(reason or "ORDER BOOK INVALID")
        should_signal = force or not self._resync_pending
        self._seeded = False
        self._ready = False
        self._snapshot_update_id = 0
        self._last_update_id = 0
        self._bids.clear()
        self._asks.clear()
        self._bid_prices.clear()
        self._ask_prices.clear()
        self._published_analysis_bids = []
        self._published_analysis_asks = []
        self._analysis_revision += 1
        self._analysis_window_dirty = True
        self._depth_bid_notional = 0.0
        self._depth_ask_notional = 0.0
        self._snapshot_bid_prices = ()
        self._snapshot_ask_prices = ()
        self._monitor_bid_coverage = False
        self._monitor_ask_coverage = False
        self._resync_pending = True
        if should_signal:
            self.invalidated.emit(self._epoch, self._revision, reason)
            self.resync_required.emit(self._epoch, self._revision)

    def _buffer_event(self, event: dict[str, Any]) -> bool:
        if len(self._buffer) >= self.MAX_BUFFERED_EVENTS:
            self._buffer.clear()
            self._signal_invalid("DEPTH BUFFER OVERFLOW", force=True)
            return False
        self._buffer.append(event)
        return True

    def _request_resync(
        self,
        reason: str,
        event: dict[str, Any] | None = None,
        *,
        force: bool = False,
    ) -> None:
        if event is not None and len(self._buffer) < self.MAX_BUFFERED_EVENTS:
            self._buffer.append(event)
        elif event is not None:
            self._buffer.clear()
            force = True
            reason = "DEPTH BUFFER OVERFLOW"
        self._signal_invalid(reason, force=force)

    @QtCore.Slot(int, int, str)
    def invalidate(self, epoch: int, revision: int, reason: str) -> None:
        epoch = int(epoch)
        revision = int(revision)
        if epoch != self._epoch or revision < self._revision:
            return



        self._revision = revision
        self._request_resync(str(reason or "ORDER BOOK INVALID"), force=True)

    def _apply_delta(self, event: dict[str, Any]) -> bool:
        bid_topology, bid_changes = self._apply_side(
            self._bids, self._bid_prices, event.get("b") or ()
        )
        ask_topology, ask_changes = self._apply_side(
            self._asks, self._ask_prices, event.get("a") or ()
        )
        self._update_depth_notional('bid', bid_topology, bid_changes)
        self._update_depth_notional('ask', ask_topology, ask_changes)
        if (
            self._changes_touch_analysis_window('bid', bid_topology, bid_changes)
            or self._changes_touch_analysis_window('ask', ask_topology, ask_changes)
        ):
            self._analysis_window_dirty = True
        self._last_update_id = int(event["u"])
        if not self._bids or not self._asks:
            self._request_resync("EMPTY ORDER BOOK", event)
            return False
        return True

    def _coverage_rebase_reason(self, best_bid: float, best_ask: float) -> str:
        """Return a proactive rebase reason before the trusted snapshot frontier is exhausted.

        Binance REST snapshots expose at most the nearest 1000 levels. A valid diff
        sequence cannot reveal untouched prices beyond that original range. We only
        monitor sides that arrived close to the 1000-level cap; shorter snapshots are
        treated as naturally shallow books rather than truncated coverage.
        """
        if self._monitor_bid_coverage and self._snapshot_bid_prices:
            remaining = bisect_right(self._snapshot_bid_prices, float(best_bid))
            if remaining <= self.COVERAGE_RESERVE_LEVELS:
                return f"DEPTH BID COVERAGE LOW ({remaining})"
        if self._monitor_ask_coverage and self._snapshot_ask_prices:
            first = bisect_left(self._snapshot_ask_prices, float(best_ask))
            remaining = len(self._snapshot_ask_prices) - first
            if remaining <= self.COVERAGE_RESERVE_LEVELS:
                return f"DEPTH ASK COVERAGE LOW ({remaining})"
        if (
            len(self._bids) > self.MAX_BOOK_LEVELS_BEFORE_REBASE
            or len(self._asks) > self.MAX_BOOK_LEVELS_BEFORE_REBASE
        ):
            return "DEPTH BOOK STATE REBASE"
        return ""

    def _emit_book(self, event: dict[str, Any] | None = None) -> None:
        if not self._ready or not self._bids or not self._asks:
            return



        if self._analysis_window_dirty or not self._published_analysis_bids or not self._published_analysis_asks:
            analysis_levels = max(
                self.PUBLISH_LEVELS,
                min(int(self._analysis_publish_levels), self.ANALYSIS_LEVELS),
            )
            bid_start = max(0, len(self._bid_prices) - analysis_levels)
            candidate_bids = [
                (price, self._bids[price])
                for index in range(len(self._bid_prices) - 1, bid_start - 1, -1)
                for price in (self._bid_prices[index],)
            ]
            ask_stop = min(len(self._ask_prices), analysis_levels)
            candidate_asks = [
                (price, self._asks[price])
                for index in range(ask_stop)
                for price in (self._ask_prices[index],)
            ]
            analysis_changed = (
                candidate_bids != self._published_analysis_bids
                or candidate_asks != self._published_analysis_asks
            )
            if analysis_changed:
                self._published_analysis_bids = candidate_bids
                self._published_analysis_asks = candidate_asks
                self._analysis_revision += 1
            self._analysis_window_dirty = False
        selected_bids = self._published_analysis_bids
        selected_asks = self._published_analysis_asks
        bids = (
            selected_bids
            if len(selected_bids) <= self.PUBLISH_LEVELS
            else selected_bids[: self.PUBLISH_LEVELS]
        )
        asks = (
            selected_asks
            if len(selected_asks) <= self.PUBLISH_LEVELS
            else selected_asks[: self.PUBLISH_LEVELS]
        )
        if not bids or not asks or bids[0][0] >= asks[0][0]:
            self._request_resync("INVALID LOCAL BBO", event)
            return

        rebase_reason = self._coverage_rebase_reason(bids[0][0], asks[0][0])
        if rebase_reason:


            self._request_resync(rebase_reason, event, force=True)
            return

        event = event or {}
        payload = {
            "s": self._symbol,
            "E": event.get("E", 0),
            "T": event.get("T", 0),
            "U": event.get("U", self._last_update_id),
            "u": self._last_update_id,
            "pu": event.get("pu", 0),
            "_bids": bids,
            "_asks": asks,
            "_analysis_bids": selected_bids,
            "_analysis_asks": selected_asks,
            "_analysis_revision": self._analysis_revision,
            "b": bids,
            "a": asks,
            "_local_book": True,
            "_depth_bid_notional": self._depth_bid_notional,
            "_depth_ask_notional": self._depth_ask_notional,
            "_depth_notional_levels": self.DEPTH_NOTIONAL_LEVELS,
            "_socket_received_wall_ms": float(
                event.get("_socket_received_wall_ms", 0.0) or 0.0
            ),
            "_socket_received_mono_ms": float(
                event.get("_socket_received_mono_ms", 0.0) or 0.0
            ),
            "_server_received_ms": float(
                event.get("_server_received_ms", 0.0) or 0.0
            ),
            "_enqueue_mono_ms": float(
                event.get("_enqueue_mono_ms", 0.0) or 0.0
            ),
            "_worker_start_mono_ms": float(
                event.get("_worker_start_mono_ms", 0.0) or 0.0
            ),
            "_worker_resume_mono_ms": float(
                event.get("_worker_resume_mono_ms", 0.0) or 0.0
            ),
            "_parser_done_mono_ms": time.perf_counter() * 1000.0,
        }
        self.parsed.emit(self._epoch, self._revision, payload)

    def _accept_event(self, event: dict[str, Any]) -> bool:
        final_id = int(event["u"])
        first_id = int(event["U"])
        previous_id = int(event["pu"])

        if final_id <= 0:
            return False

        if self._ready:


            if final_id <= self._last_update_id:
                return False
            if previous_id != self._last_update_id:
                self._request_resync("DEPTH SEQUENCE GAP", event)
                return False
            return self._apply_delta(event)

        if final_id < self._snapshot_update_id:
            return False


        if first_id <= self._snapshot_update_id <= final_id:
            if not self._apply_delta(event):
                return False
            self._ready = True
            self._resync_pending = False
            return True
        if first_id > self._snapshot_update_id:
            self._request_resync("DEPTH SNAPSHOT BRIDGE GAP", event)
        return False

    @QtCore.Slot(int, int, object)
    def seed_snapshot(self, epoch: int, revision: int, payload: object) -> None:
        epoch = int(epoch)
        revision = int(revision)
        if (
            epoch != self._epoch
            or revision != self._revision
            or not isinstance(payload, dict)
        ):
            return
        snapshot_id = _strict_int(payload.get("lastUpdateId") or payload.get("u"))
        snapshot_symbol = str(payload.get("s") or self._symbol).upper()
        bids = self._snapshot_side(payload.get("b") or payload.get("bids"))
        asks = self._snapshot_side(payload.get("a") or payload.get("asks"))
        if (
            snapshot_id is None or snapshot_id <= 0
            or snapshot_symbol != self._symbol
            or bids is None or asks is None
            or max(bids) >= min(asks)
        ):
            self._request_resync("INVALID DEPTH SNAPSHOT", force=True)
            return

        self._bids = bids
        self._asks = asks
        self._bid_prices = sorted(bids)
        self._ask_prices = sorted(asks)
        self._published_analysis_bids = []
        self._published_analysis_asks = []
        self._analysis_revision += 1
        self._analysis_window_dirty = True
        self._depth_bid_notional = self._depth_notional_for_side('bid')
        self._depth_ask_notional = self._depth_notional_for_side('ask')
        self._snapshot_bid_prices = tuple(self._bid_prices)
        self._snapshot_ask_prices = tuple(self._ask_prices)
        self._monitor_bid_coverage = len(self._snapshot_bid_prices) >= self.COVERAGE_MONITOR_MIN_LEVELS
        self._monitor_ask_coverage = len(self._snapshot_ask_prices) >= self.COVERAGE_MONITOR_MIN_LEVELS
        self._snapshot_update_id = snapshot_id
        self._last_update_id = snapshot_id
        self._seeded = True
        self._ready = False
        self._resync_pending = False

        buffered = list(self._buffer)
        self._buffer.clear()
        bridge_started = time.perf_counter()
        if buffered:
            pass
        try:
            latest_applied: dict[str, Any] | None = None
            for index, event in enumerate(buffered):



                event["_worker_resume_mono_ms"] = time.perf_counter() * 1000.0
                if self._accept_event(event):
                    latest_applied = event
                if not self._seeded:
                    for remaining in buffered[index + 1 :]:
                        if not self._buffer_event(remaining):
                            break
                    return

            if self._ready and latest_applied is not None:
                self._emit_book(latest_applied)
        finally:
            if buffered:
                pass

    @QtCore.Slot(int, str, str, object)
    def parse(
        self,
        epoch: int,
        message: str,
        symbol: str,
        timing: object,
    ) -> None:
        worker_start_mono_ms = time.perf_counter() * 1000.0
        epoch = int(epoch)
        if epoch < self._epoch:
            return
        if epoch > self._epoch:
            self._reset_state(epoch, 0, symbol)

        arrival_mono_ms = 0.0
        enqueue_mono_ms = 0.0
        if isinstance(timing, (tuple, list)):
            if len(timing) >= 2:
                arrival_mono_ms = safe_float(timing[1])
            if len(timing) >= 4:
                enqueue_mono_ms = safe_float(timing[3])
        queue_age_ms = 0.0
        if arrival_mono_ms > 0.0:


            queue_age_ms = max(0.0, worker_start_mono_ms - arrival_mono_ms)
        if enqueue_mono_ms >= arrival_mono_ms > 0.0:
            pass
        if worker_start_mono_ms >= enqueue_mono_ms > 0.0:
            pass

        event = self._decode_delta(message, symbol, timing)
        if event is None:
            return
        event["_worker_start_mono_ms"] = worker_start_mono_ms

        if queue_age_ms > self.MAX_PARSER_QUEUE_AGE_MS:
            self._request_resync("DEPTH PARSER BACKPRESSURE", event, force=True)
            return

        if not self._seeded:
            self._buffer_event(event)
            return

        if self._accept_event(event):
            self._emit_book(event)





import json
import random
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

import numpy as np
from PySide6 import QtCore, QtNetwork
from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtWebSockets import QWebSocket

from ..models import CandlePages, _candle_matrix_from_objects
from ..chart.analysis import LatestJob, run_analysis
from ..networking.binance import BinanceRest
from ..constants import (
    ANALYSIS_CACHE_LIMIT,
    ANALYSIS_CACHE_REFRESH_SECONDS,
    CHART_CACHE_LIMIT,
    CHART_CACHE_REFRESH_SECONDS,
    DEFAULT_INTERVAL,
    DEFAULT_SYMBOL,
    DEPTH_FALLBACK_INTERVAL_MS,
    INTERVAL_SECONDS,
    MAIN_WS,
    MARKET_OVERVIEW_REFRESH_MS,
    MAX_CHART_CANDLES,
    TEST_WS,
)
from ..models import Candle, shift_candle_time
from ..networking.binance import ApiTask, launch_task
from ..networking.binance import BINANCE_RATE_LIMITER
from ..models import safe_float




DEPTH_SNAPSHOT_RETRY_DELAYS_MS = (400, 800, 1600)
DEPTH_PROGRESS_STALL_SECONDS = 10.0
PUBLIC_SOCKET_DATA_STALE_SECONDS = 45.0
MARKET_SOCKET_DATA_STALE_SECONDS = 8.0
TICKER_SOCKET_DATA_STALE_SECONDS = 8.0


def _depth_progress_stalled(
    *,
    book_valid: bool,
    public_connected: bool,
    last_depth_event: float,
    last_book_ticker_event: float,
    now: float,
    stall_seconds: float = DEPTH_PROGRESS_STALL_SECONDS,
) -> bool:
    """Detect depth silence while BBO makes local progress, without comparing IDs."""
    if (
        not book_valid
        or not public_connected
        or last_depth_event <= 0.0
    ):
        return False
    depth_age = float(now) - float(last_depth_event)
    return (
        depth_age >= float(stall_seconds)
        and last_book_ticker_event > last_depth_event
        and float(now) - last_book_ticker_event <= float(stall_seconds)
    )


def _prepare_chart_cache(base, incoming, live, requested_at):
    """Merge history on a worker and publish copy-on-write candle pages.

    The chart prepares its own matrix/LOD off-thread. Keeping a second rolling
    matrix in the hub duplicated that work and forced large copies on live ticks.
    """
    rows = {candle.time: candle for candle in base.get("candles", ())}
    rows.update((candle.time, candle) for candle in incoming.get("candles", ()))
    latest = max(rows, default=0)
    for stamp, candle in live:
        if stamp >= requested_at or candle.time > latest:
            rows[candle.time] = candle
    result = {**base, **incoming}
    result["candles"] = CandlePages(sorted(rows.values(), key=lambda candle: candle.time)[-MAX_CHART_CANDLES:])
    result.pop("candle_matrix", None)
    result.pop("_rolling_matrix", None)
    result.pop("_rolling_start", None)
    return result


def _prepare_chart_cache_job(base, incoming, live, requested_at):
    return run_analysis(_prepare_chart_cache, base, incoming, live, requested_at)


class MarketDataHub(QtCore.QObject):
    _socket_drain_request = Signal()
    _depth_drain_request = Signal()
    _depth_reset_request = Signal(int, int, str)
    _depth_seed_request = Signal(int, int, object)
    _depth_invalidate_request = Signal(int, int, str)
    _depth_capacity_request = Signal(int)

    universe_ready = Signal(object)
    bootstrap_ready = Signal(object)
    analysis_ready = Signal(object)
    interest_history_ready = Signal(object)
    ticker_batch = Signal(object)
    kline = Signal(object)
    depth = Signal(object)
    book_ticker = Signal(object)
    mark_price = Signal(object)
    liquidation = Signal(object)
    trade_batch = Signal(object)
    interest = Signal(object)
    status = Signal(str, bool)
    book_validity = Signal(bool, str)
    trade_stream_status = Signal(bool, str)
    problem = Signal(str)

    def __init__(
        self,
        testnet: bool = False,
        parent: QtCore.QObject | None = None,
        *,
        chart_only: bool = False,
        db: Any | None = None,
        diagnostics: DiagnosticsPort | None = None,
    ):
        super().__init__(parent)
        self.testnet = testnet
        self._diagnostics = diagnostics
        self.chart_only = bool(chart_only)
        self.db = db
        self.rest = BinanceRest(testnet)
        self.ws_base = TEST_WS if testnet else MAIN_WS
        self.symbol = DEFAULT_SYMBOL
        self.interval = DEFAULT_INTERVAL
        self.generation = 0


        self.depth_epoch = 0



        self.depth_revision = 0
        self.market_epoch = 0
        self._socket_generations: dict[str, int] = {}




        self.orderbook_streaming_enabled = True
        self._orderbook_depth_capacity = _DepthParserWorker.PUBLISH_LEVELS
        self._market_socket_has_agg_trade = False
        self._trade_stream_status_last: tuple[bool, str] | None = None
        self.public_socket: QWebSocket | None = None
        self.market_socket: QWebSocket | None = None
        self.ticker_socket: QWebSocket | None = None
        self.ticker_symbols: tuple[str, ...] = ()
        self._valid_ticker_symbols: frozenset[str] = frozenset()
        self.connected = {"public": False, "market": False}
        self.last_depth_event = 0.0
        self.last_depth_update_id = 0
        self.last_book_ticker_event = 0.0
        self.last_book_ticker_update_id = 0
        self._live_candles: dict[int, tuple[float, Candle]] = {}
        self._kline_event_time = 0.0
        self._pending_reads: set[tuple[Any, ...]] = set()


        self._socket_activity: dict[str, float] = {}
        self._socket_data_activity: dict[str, float] = {}
        self._socket_started: dict[str, float] = {}
        self._socket_healthy_since: dict[str, float] = {}
        self._disconnect_tokens: dict[str, int] = {}
        self._retry_attempts = {"public": 0, "market": 0, "ticker": 0}
        self._chart_needs_repair = False
        self._chart_gap_start: float | None = None
        self._chart_repair_revision = 0
        self._chart_repair_attempts = 0
        self._chart_repair_timer = QTimer(self)
        self._chart_repair_timer.setSingleShot(True)
        self._chart_repair_timer.timeout.connect(self._repair_chart_history)
        self._ticker_live_at = 0.0
        self.watchdog_timer = QTimer(self)
        self.watchdog_timer.setInterval(5_000)
        self.watchdog_timer.timeout.connect(self._check_sockets)
        self._socket_ping_at: dict[str, float] = {}
        self.depth_snapshot_epoch: int | None = None



        self._depth_snapshot_seeded_epoch: int | None = None
        self._depth_snapshot_seeded_at = 0.0
        self._depth_force_refresh_epoch: int | None = None
        self._depth_snapshot_retry_epoch: int | None = None
        self._depth_snapshot_retry_count = 0
        self._depth_resync_started_mono = 0.0
        self.book_valid = False
        self._book_invalid_reason = "STARTUP"
        self._clock_status_cache: dict[str, Any] = {}
        self._clock_status_cache_mono = 0.0
        self._deferred_analysis_request: tuple[int, str, str] | None = None
        self.stopping = False
        self.suspended = False
        self.started = False
        self.tasks: set[ApiTask] = set()
        self._lifecycle = 0
        self.task_pool = QtCore.QThreadPool(self)
        self.task_pool.setMaxThreadCount(4)
        self.task_pool.setExpiryTimeout(30_000)
        self._background_pool = QtCore.QThreadPool(self)
        self._background_pool.setMaxThreadCount(2)
        self._background_pool.setExpiryTimeout(30_000)
        self._parser_thread: QtCore.QThread | None = None
        self._parser_worker: _SocketParserWorker | None = None
        self._depth_parser_thread: QtCore.QThread | None = None
        self._depth_parser_worker: _DepthParserWorker | None = None
        self._retiring_threads: list[QtCore.QThread] = []
        self.chart_cache: OrderedDict[tuple[str, str], tuple[float, dict[str, Any]]] = OrderedDict()
        self.analysis_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._cache_prepare_queue = deque()
        self._cache_prepare_active = None
        self._cache_prepare_serial = 0
        self._cache_prepare_job = LatestJob(QtCore.QThreadPool.globalInstance(), self, priority=1)
        self._cache_prepare_job.ready.connect(self._cache_prepared)
        self._cache_prepare_job.failed.connect(self._cache_prepare_failed)




        self.chart_prefetch_symbols: tuple[str, ...] = ()
        self._chart_prefetch_queue: list[tuple[str, str]] = []
        self._chart_prefetch_active: tuple[str, str] | None = None
        self._interaction_priority = False
        self._chart_prefetch_timer = QTimer(self)
        self._chart_prefetch_timer.setSingleShot(True)
        self._chart_prefetch_timer.setInterval(900)
        self._chart_prefetch_timer.timeout.connect(self._run_next_chart_prefetch)
        self.oi_timer = QTimer(self)
        self.oi_timer.setInterval(10_000)
        self.oi_timer.timeout.connect(self.refresh_interest)
        self.analysis_timer = QTimer(self)
        self.analysis_timer.setInterval(300_000)
        self.analysis_timer.timeout.connect(self.refresh_analysis)
        self.overview_timer = QTimer(self)
        self.overview_timer.setInterval(MARKET_OVERVIEW_REFRESH_MS)
        self.overview_timer.timeout.connect(self.refresh_market_overview)
        self.depth_fallback_timer = QTimer(self)
        self.depth_fallback_timer.setInterval(DEPTH_FALLBACK_INTERVAL_MS)
        self.depth_fallback_timer.timeout.connect(self.refresh_depth_fallback)
        self.public_reconnect_timer = QTimer(self)
        self.public_reconnect_timer.setSingleShot(True)
        self.public_reconnect_timer.timeout.connect(
            lambda: self._reconnect_leg("public")
        )
        self.market_reconnect_timer = QTimer(self)
        self.market_reconnect_timer.setSingleShot(True)
        self.market_reconnect_timer.timeout.connect(
            lambda: self._reconnect_leg("market")
        )
        self.ticker_reconnect_timer = QTimer(self)
        self.ticker_reconnect_timer.setSingleShot(True)
        self.ticker_reconnect_timer.setInterval(2200)
        self.ticker_reconnect_timer.timeout.connect(self._reconnect_tickers)
        self._subscribed_interval = self.interval
        self.subscription_timer = QTimer(self)
        self.subscription_timer.setSingleShot(True)
        self.subscription_timer.setInterval(500)
        self.subscription_timer.timeout.connect(self._resubscribe_kline)




        self.universe_loaded = False
        self.universe_retry_timer = QTimer(self)
        self.universe_retry_timer.setSingleShot(True)
        self.universe_retry_timer.setInterval(5000)
        self.universe_retry_timer.timeout.connect(self._load_universe)

    def _ensure_parser_thread(self) -> None:


        if self.chart_only:
            return

        if self._parser_thread is None:
            thread = QtCore.QThread()
            thread.setObjectName("nightwatch-market-parser")
            worker = _SocketParserWorker()
            worker.moveToThread(thread)

            self._socket_drain_request.connect(
                worker.drain,
                QtCore.Qt.ConnectionType.QueuedConnection,
            )
            worker.parsed.connect(
                self._handle_parsed_socket_message,
                QtCore.Qt.ConnectionType.QueuedConnection,
            )
            worker.backpressure.connect(
                self._handle_parser_backpressure,
                QtCore.Qt.ConnectionType.QueuedConnection,
            )
            thread.finished.connect(worker.deleteLater)
            thread.start()

            self._parser_thread = thread
            self._parser_worker = worker

        if self._depth_parser_thread is None:
            depth_thread = QtCore.QThread()
            depth_thread.setObjectName("nightwatch-depth-parser")
            depth_worker = _DepthParserWorker()
            depth_worker.moveToThread(depth_thread)

            self._depth_drain_request.connect(
                depth_worker.drain,
                QtCore.Qt.ConnectionType.QueuedConnection,
            )
            self._depth_reset_request.connect(
                depth_worker.reset_epoch,
                QtCore.Qt.ConnectionType.QueuedConnection,
            )
            self._depth_seed_request.connect(
                depth_worker.seed_snapshot,
                QtCore.Qt.ConnectionType.QueuedConnection,
            )
            self._depth_invalidate_request.connect(
                depth_worker.invalidate,
                QtCore.Qt.ConnectionType.QueuedConnection,
            )
            self._depth_capacity_request.connect(
                depth_worker.set_analysis_publish_levels,
                QtCore.Qt.ConnectionType.QueuedConnection,
            )
            depth_worker.parsed.connect(
                self._handle_parsed_depth,
                QtCore.Qt.ConnectionType.QueuedConnection,
            )
            depth_worker.invalidated.connect(
                self._handle_depth_invalidated,
                QtCore.Qt.ConnectionType.QueuedConnection,
            )
            depth_worker.resync_required.connect(
                self._handle_depth_resync,
                QtCore.Qt.ConnectionType.QueuedConnection,
            )
            depth_thread.finished.connect(depth_worker.deleteLater)
            depth_thread.start()

            self._depth_parser_thread = depth_thread
            self._depth_parser_worker = depth_worker
            self._depth_capacity_request.emit(self._orderbook_depth_capacity)

    @QtCore.Slot()
    def _release_retiring_thread(self) -> None:
        """Delete a worker QThread only after Qt confirms it has stopped."""
        thread = self.sender()
        if not isinstance(thread, QtCore.QThread):
            return
        if thread not in self._retiring_threads:
            return
        self._retiring_threads.remove(thread)
        thread.deleteLater()

    def _stop_worker_thread(
        self,
        signal: QtCore.SignalInstance,
        worker_slot: Callable[..., Any] | None,
        worker_signal: QtCore.SignalInstance | None,
        receiver: Callable[..., Any] | None,
        thread: QtCore.QThread | None,
    ) -> None:
        if worker_slot is not None:
            try:
                signal.disconnect(worker_slot)
            except (RuntimeError, TypeError):
                pass
        if worker_signal is not None and receiver is not None:
            try:
                worker_signal.disconnect(receiver)
            except (RuntimeError, TypeError):
                pass
        if thread is None:
            return




        thread.requestInterruption()
        thread.quit()
        if thread.wait(1500):
            thread.deleteLater()
            return




        if thread not in self._retiring_threads:
            self._retiring_threads.append(thread)
            thread.finished.connect(
                self._release_retiring_thread,
                QtCore.Qt.ConnectionType.QueuedConnection,
            )


            if not thread.isRunning() and thread in self._retiring_threads:
                self._retiring_threads.remove(thread)
                thread.deleteLater()

    def _stop_parser_thread(self) -> None:
        parser_thread = self._parser_thread
        parser_worker = self._parser_worker
        depth_thread = self._depth_parser_thread
        depth_worker = self._depth_parser_worker

        self._parser_thread = None
        self._parser_worker = None
        self._depth_parser_thread = None
        self._depth_parser_worker = None

        if parser_worker is not None:
            try:
                self._socket_drain_request.disconnect(parser_worker.drain)
            except (RuntimeError, TypeError):
                pass
            try:
                parser_worker.backpressure.disconnect(self._handle_parser_backpressure)
            except (RuntimeError, TypeError):
                pass
        self._stop_worker_thread(
            self._socket_drain_request,
            None,
            parser_worker.parsed if parser_worker is not None else None,
            self._handle_parsed_socket_message,
            parser_thread,
        )
        if depth_worker is not None:
            for signal, slot in (
                (self._depth_reset_request, depth_worker.reset_epoch),
                (self._depth_seed_request, depth_worker.seed_snapshot),
                (self._depth_invalidate_request, depth_worker.invalidate),
            ):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
            for worker_signal, receiver in (
                (depth_worker.invalidated, self._handle_depth_invalidated),
                (depth_worker.resync_required, self._handle_depth_resync),
            ):
                try:
                    worker_signal.disconnect(receiver)
                except (RuntimeError, TypeError):
                    pass
        if depth_worker is not None:
            try:
                self._depth_drain_request.disconnect(depth_worker.drain)
            except (RuntimeError, TypeError):
                pass


        self._stop_worker_thread(
            self._depth_reset_request,
            None,
            depth_worker.parsed if depth_worker is not None else None,
            self._handle_parsed_depth,
            depth_thread,
        )


    @QtCore.Slot(str, int, str)
    def _handle_parser_backpressure(
        self,
        kind: str,
        generation: int,
        reason: str,
    ) -> None:
        """Fail closed for a non-depth parser gap instead of replaying stale data."""
        kind = str(kind)
        generation = int(generation)
        if kind == "public":
            current = self.depth_epoch
        elif kind == "market" and not self.chart_only:
            current = self.market_epoch
        else:
            current = self.generation
        if generation != current or self.stopping or self.suspended:
            return
        reason = str(reason or "SOCKET PARSER BACKPRESSURE")
        if kind == 'market' and not self.chart_only:
            self._publish_trade_stream_status(False, reason)



        self._socket_error(kind, self.generation, reason)


    def _publish_trade_stream_status(self, active: bool | None=None, reason: str='') -> None:
        if self.chart_only:
            return
        if active is None:
            active = bool(self.orderbook_streaming_enabled and self.connected.get('market', False) and self._market_socket_has_agg_trade)
        if active:
            normalized_reason = 'READY'
        elif reason:
            normalized_reason = str(reason).strip().upper()
        elif not self.orderbook_streaming_enabled:
            normalized_reason = 'PAUSED'
        elif not self.connected.get('market', False):
            normalized_reason = 'MARKET STREAM OFFLINE'
        elif not self._market_socket_has_agg_trade:
            normalized_reason = 'TRADE STREAM UNSUBSCRIBED'
        else:
            normalized_reason = 'TRADE STREAM UNAVAILABLE'
        state = (bool(active), normalized_reason)
        if state == self._trade_stream_status_last:
            return
        self._trade_stream_status_last = state
        self.trade_stream_status.emit(state[0], state[1])

    def execution_book_is_fresh(self, now: float | None = None) -> bool:
        if not self.book_valid or not self.connected.get("public", False) or self.last_depth_event <= 0:
            return False
        current = time.monotonic() if now is None else now
        return book_data_is_fresh(
            current - self.last_depth_event,
            current - max(self.last_depth_event, self.last_book_ticker_event),
        )

    def _set_book_valid(self, valid: bool, reason: str) -> None:
        """Publish canonical local-book validity independently of socket status."""
        valid = bool(valid)
        reason = str(reason or ("READY" if valid else "ORDER BOOK INVALID"))
        changed = valid != self.book_valid or reason != self._book_invalid_reason
        self.book_valid = valid
        self._book_invalid_reason = reason
        if changed:
            if not valid:
                reason_upper = reason.upper()
                category = next((key for token, key in (
                    ("OVERFLOW", "overflow"), ("BACKPRESSURE", "backpressure"),
                    ("SEQUENCE GAP", "sequence_gap"), ("BRIDGE", "snapshot_bridge"),
                    ("BBO", "invalid_bbo"), ("CROSSED", "crossed_book"),
                    ("COVERAGE", "coverage"), ("STALLED", "stream_stalled"),
                    ("SNAPSHOT", "snapshot"), ("OFFLINE", "offline"), ("STALE", "stale"),
                    ("RATE LIMITED", "rate_limited"), ("SYNC", "syncing"),
                    ("PAUSED", "paused"), ("CLOSED", "closed"),
                ) if token in reason_upper), "other")
                if self._diagnostics is not None:
                    self._diagnostics.increment(f"market.depth.invalid.{category}")
                    self._diagnostics.warning("MARKET DEPTH", reason)
            self.book_validity.emit(valid, reason)
        if valid:
            self._flush_deferred_analysis()

    def _invalidate_depth_book(
        self,
        reason: str,
        *,
        freeze_worker: bool,
        request_snapshot: bool = True,
    ) -> None:
        if self.chart_only or self.depth_epoch <= 0:
            return
        reason = str(reason or "ORDER BOOK INVALID")



        if freeze_worker:
            self.depth_revision += 1
        revision = self.depth_revision
        if self._depth_resync_started_mono <= 0.0:
            self._depth_resync_started_mono = time.perf_counter()
        self._set_book_valid(False, reason)
        self._depth_snapshot_seeded_epoch = None
        self._depth_snapshot_seeded_at = 0.0
        self.last_depth_update_id = 0
        if freeze_worker:


            self._depth_snapshot_retry_epoch = None
            self._depth_snapshot_retry_count = 0
        if freeze_worker and self._depth_parser_worker is not None:


            self._depth_invalidate_request.emit(
                self.depth_epoch, revision, reason
            )
        elif request_snapshot and self.connected.get("public", False):
            self._load_depth_snapshot(
                self.depth_epoch, revision=revision, force=True
            )

    @QtCore.Slot(int, int, str)
    def _handle_depth_invalidated(
        self, epoch: int, revision: int, reason: str
    ) -> None:
        if (
            epoch != self.depth_epoch
            or revision != self.depth_revision
            or self.chart_only
            or not self.orderbook_streaming_enabled
        ):
            return
        self._set_book_valid(False, str(reason or "ORDER BOOK INVALID"))
        if self._depth_resync_started_mono <= 0.0:
            self._depth_resync_started_mono = time.perf_counter()
        self._depth_snapshot_seeded_epoch = None
        self._depth_snapshot_seeded_at = 0.0
        self.last_depth_update_id = 0

    def _begin_depth_epoch(self, symbol: str) -> int:
        """Start a new local-book lifecycle only when the depth stream changes."""
        self.depth_epoch += 1
        self.depth_revision += 1
        epoch = self.depth_epoch
        revision = self.depth_revision
        self.last_depth_event = 0.0
        self.last_depth_update_id = 0
        self.last_book_ticker_event = 0.0
        self.last_book_ticker_update_id = 0
        self.depth_snapshot_epoch = None
        self._depth_snapshot_seeded_epoch = None
        self._depth_snapshot_seeded_at = 0.0
        self._depth_force_refresh_epoch = None
        self._depth_snapshot_retry_epoch = None
        self._depth_snapshot_retry_count = 0
        self._depth_resync_started_mono = 0.0
        self._set_book_valid(False, "SYNCING")
        if self._depth_parser_worker is not None and not self.chart_only:
            self._depth_reset_request.emit(
                epoch, revision, str(symbol).upper()
            )
        return epoch

    def _check_depth_progress_health(self, now: float | None = None) -> bool:
        """Repair on sustained depth-stream silence using only local progress."""
        if self.chart_only or not self.orderbook_streaming_enabled:
            return False
        now = time.monotonic() if now is None else float(now)
        if not _depth_progress_stalled(
            book_valid=self.book_valid,
            public_connected=self.connected.get("public", False),
            last_depth_event=self.last_depth_event,
            last_book_ticker_event=self.last_book_ticker_event,
            now=now,
        ):
            return False
        if self._diagnostics is not None:
            self._diagnostics.increment("market.depth.resync.stream_stalled")
        self._invalidate_depth_book("DEPTH STREAM STALLED", freeze_worker=True)
        return True

    def _server_clock_now_ms(self) -> float:
        """Return cached Binance server time only when the background sync is fresh."""
        fresh = getattr(self.rest, "has_fresh_time_offset", None)
        cached = getattr(self.rest, "cached_timestamp_ms", None)
        if not callable(cached):
            return 0.0
        try:
            if callable(fresh) and not fresh(90.0):
                return 0.0
            return float(cached())
        except (RuntimeError, TypeError, ValueError):
            return 0.0

    def server_clock_status(self) -> dict[str, Any]:
        """Return clock-estimator health without allowing telemetry to raise."""
        status = getattr(self.rest, "server_clock_status", None)
        if not callable(status):
            return {"synced": False, "fresh": False, "last_error": "unavailable"}
        try:
            return dict(status())
        except Exception as exc:  # noqa: BLE001 - telemetry must be fail-open
            return {
                "synced": False,
                "fresh": False,
                "last_error": f"{type(exc).__name__}: {exc}",
            }

    def _cached_server_clock_status(self) -> tuple[dict[str, Any], bool]:
        """Return clock telemetry refreshed at most once per second."""
        now = time.monotonic()
        if (
            self._clock_status_cache
            and now - self._clock_status_cache_mono < 1.0
        ):
            return self._clock_status_cache, False
        clock = self.server_clock_status()
        self._clock_status_cache = clock
        self._clock_status_cache_mono = now
        return clock, True

    def _queue_socket_message(
        self,
        kind: str,
        generation: int,
        message: str,
    ) -> None:
        if generation != self._socket_generations.get(kind):
            return



        arrival_wall_ms = time.time() * 1000.0
        arrival_mono_ms = time.perf_counter() * 1000.0

        now_mono = time.monotonic()
        self._socket_activity[kind] = now_mono
        self._socket_data_activity[kind] = now_mono

        if not self.chart_only:
            self._ensure_parser_thread()

        if kind == "public":
            if not self.orderbook_streaming_enabled:
                return
            epoch = self.depth_epoch
            if epoch <= 0:
                return






            wrapper_end = min(len(message), 256)
            if (
                message.find("@bookTicker", 0, wrapper_end) >= 0
                or message.find("@bookticker", 0, wrapper_end) >= 0
            ):
                if self._parser_worker is not None:
                    schedule_drain = self._parser_worker.enqueue(
                        kind,
                        epoch,
                        message,
                        self.symbol,
                        self.interval,
                        self.ticker_symbols,
                        self._valid_ticker_symbols,
                        arrival_mono_ms,
                    )
                    if schedule_drain:
                        self._socket_drain_request.emit()
                else:
                    packets = _decode_socket_payload(
                        kind,
                        message,
                        self.symbol,
                        self.interval,
                        self.ticker_symbols,
                        self._valid_ticker_symbols,
                    )
                    if packets:
                        self._handle_parsed_socket_message(kind, epoch, packets)
                return
            if self._depth_parser_worker is not None:



                server_received_ms = self._server_clock_now_ms()
                enqueue_mono_ms = time.perf_counter() * 1000.0
                schedule_drain = self._depth_parser_worker.enqueue(
                    epoch,
                    message,
                    self.symbol,
                    (
                        arrival_wall_ms,
                        arrival_mono_ms,
                        server_received_ms,
                        enqueue_mono_ms,
                    ),
                )
                if schedule_drain:
                    self._depth_drain_request.emit()
                return







        parser_generation = (
            self.market_epoch
            if kind == "market" and not self.chart_only
            else self.generation if self.chart_only else generation
        )
        if self._parser_worker is not None:
            schedule_drain = self._parser_worker.enqueue(
                kind,
                parser_generation,
                message,
                self.symbol,
                self.interval,
                self.ticker_symbols,
                self._valid_ticker_symbols,
                arrival_mono_ms,
            )
            if schedule_drain:
                self._socket_drain_request.emit()
            return

        packets = _decode_socket_payload(
            kind,
            message,
            self.symbol,
            self.interval,
            self.ticker_symbols,
            self._valid_ticker_symbols,
        )
        if packets:


            annotated = []
            parser_done_mono_ms = time.perf_counter() * 1000.0
            server_arrival_ms = self._server_clock_now_ms()
            for packet_type, payload in packets:
                if packet_type == "depth" and isinstance(payload, dict):
                    payload["_socket_received_wall_ms"] = arrival_wall_ms
                    payload["_socket_received_mono_ms"] = arrival_mono_ms
                    payload["_server_received_ms"] = server_arrival_ms
                    payload["_parser_done_mono_ms"] = parser_done_mono_ms
                annotated.append((packet_type, payload))
            self._handle_parsed_socket_message(
                kind,
                self.depth_epoch if kind == "public" else parser_generation,
                tuple(annotated),
            )

    @QtCore.Slot(int, int)
    def _handle_depth_resync(self, epoch: int, revision: int) -> None:
        if (
            epoch != self.depth_epoch
            or revision != self.depth_revision
            or self.stopping
            or self.suspended
            or self.chart_only
            or not self.orderbook_streaming_enabled
        ):
            return
        self._set_book_valid(False, "DEPTH RESYNC")
        if self._depth_resync_started_mono <= 0.0:
            self._depth_resync_started_mono = time.perf_counter()
        self._depth_snapshot_seeded_epoch = None
        self._depth_snapshot_seeded_at = 0.0
        self.last_depth_update_id = 0
        self._load_depth_snapshot(epoch, revision=revision, force=True)

    @QtCore.Slot(int, int, object)
    def _handle_parsed_depth(
        self,
        epoch: int,
        revision: int,
        payload: object,
    ) -> None:
        if (
            epoch != self.depth_epoch
            or revision != self.depth_revision
            or not isinstance(payload, dict)
        ):
            return
        self._dispatch_depth_payload(epoch, revision, payload)

    def _dispatch_depth_payload(
        self,
        epoch: int,
        revision: int,
        payload: dict[str, Any],
    ) -> None:
        if epoch != self.depth_epoch or revision != self.depth_revision:
            return
        if (
            self.stopping
            or self.suspended
            or not self.orderbook_streaming_enabled
            or not self.connected.get("public", False)
        ):
            return
        received_ms = safe_float(payload.get("_socket_received_mono_ms"))
        if received_ms <= 0.0:
            received_ms = safe_float(payload.get("_parser_done_mono_ms"))
        if received_ms > 0.0 and time.perf_counter() * 1000.0 - received_ms > _DepthParserWorker.MAX_PARSER_QUEUE_AGE_MS:
            self._invalidate_depth_book("DEPTH GUI BACKPRESSURE", freeze_worker=True)
            return
        if str(payload.get("s") or "").upper() != self.symbol:
            return
        try:
            update_id = int(payload.get("u") or 0)
        except (TypeError, ValueError, OverflowError):
            return
        if update_id <= 0:
            return
        if update_id <= self.last_depth_update_id:
            return
        bids = payload.get("_bids")
        asks = payload.get("_asks")
        if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks:
            self._invalidate_depth_book("EMPTY ORDER BOOK", freeze_worker=True)
            return
        try:
            best_bid = float(bids[0][0])
            best_ask = float(asks[0][0])
        except (TypeError, ValueError, IndexError):
            self._invalidate_depth_book("INVALID BBO", freeze_worker=True)
            return
        if not (best_bid > 0.0 and best_ask > best_bid):
            self._invalidate_depth_book("CROSSED ORDER BOOK", freeze_worker=True)
            return
        self.last_depth_update_id = update_id
        self.last_depth_event = time.monotonic()
        gui_dispatch_mono_ms = time.perf_counter() * 1000.0
        parser_done_mono_ms = safe_float(payload.get("_parser_done_mono_ms"))
        if parser_done_mono_ms > 0.0 and gui_dispatch_mono_ms >= parser_done_mono_ms:
            pass
        if self._depth_snapshot_retry_epoch == epoch:
            self._depth_snapshot_retry_epoch = None
            self._depth_snapshot_retry_count = 0
        payload["_gui_dispatch_mono_ms"] = gui_dispatch_mono_ms



        socket_mono_ms = safe_float(payload.get("_socket_received_mono_ms"))
        enqueue_mono_ms = safe_float(payload.get("_enqueue_mono_ms"))
        worker_start_mono_ms = safe_float(payload.get("_worker_start_mono_ms"))
        worker_resume_mono_ms = safe_float(payload.get("_worker_resume_mono_ms"))
        processing_start_mono_ms = max(worker_start_mono_ms, worker_resume_mono_ms)
        if processing_start_mono_ms > 0.0 and parser_done_mono_ms >= processing_start_mono_ms:
            pass
        if parser_done_mono_ms > 0.0 and gui_dispatch_mono_ms >= parser_done_mono_ms:
            pass

        event_ms = safe_float(payload.get("E"))
        server_received_ms = safe_float(payload.get("_server_received_ms"))
        clock, clock_refreshed = self._cached_server_clock_status()
        upstream_ms: float | None = None
        if bool(clock.get("fresh")) and event_ms > 0.0 and server_received_ms > 0.0:
            candidate = server_received_ms - event_ms
            if -100.0 <= candidate < 60_000.0:
                upstream_ms = max(0.0, candidate)
            else:
                pass
        elif event_ms > 0.0:
            pass

        if clock_refreshed:
            gauges = {
                gauge_key: clock[source_key]
                for gauge_key, source_key in (
                    ("clock.offset_ms", "offset_ms"),
                    ("clock.age_s", "age_s"),
                    ("clock.rtt_ms", "rtt_ms"),
                    ("clock.sync_count", "sync_count"),
                    ("clock.last_error", "last_error"),
                )
                if source_key in clock and clock[source_key] is not None
            }

        payload["_latency"] = {
            "event_ms": event_ms,
            "server_received_ms": server_received_ms,
            "socket_mono_ms": socket_mono_ms,
            "enqueue_mono_ms": enqueue_mono_ms,
            "worker_start_mono_ms": worker_start_mono_ms,
            "worker_resume_mono_ms": worker_resume_mono_ms,
            "parser_done_mono_ms": parser_done_mono_ms,
            "gui_dispatch_mono_ms": gui_dispatch_mono_ms,
            "upstream_ms": upstream_ms,
            "clock": clock,
        }

        if self._depth_resync_started_mono > 0.0:
            self._depth_resync_started_mono = 0.0
        self._set_book_valid(True, "READY")
        self.depth.emit(payload)


    @QtCore.Slot(str, int, object)
    def _handle_parsed_socket_message(
        self,
        kind: str,
        generation: int,
        packets: object,
    ) -> None:
        if self.stopping or self.suspended:
            return
        if kind == "public":
            if not self.orderbook_streaming_enabled or generation != self.depth_epoch:
                return
        elif kind == "market" and not self.chart_only:
            if generation != self.market_epoch:
                return
        elif kind != "ticker" and generation != self.generation:
            return

        for packet_type, payload in packets:
            if packet_type == "book_ticker":
                if not self.orderbook_streaming_enabled:
                    continue
                if (
                    isinstance(payload, dict)
                    and str(payload.get("s") or "").upper() == self.symbol
                ):
                    try:
                        update_id = int(payload.get("u") or 0)
                    except (TypeError, ValueError, OverflowError):
                        update_id = 0
                    if update_id > self.last_book_ticker_update_id:
                        self.last_book_ticker_update_id = update_id
                        self.last_book_ticker_event = time.monotonic()
                        self._check_depth_progress_health(self.last_book_ticker_event)
                    self.book_ticker.emit(payload)
                continue
            if packet_type == "tickers":
                if payload:
                    self._ticker_live_at = time.monotonic()
                    self.ticker_batch.emit(payload)
                continue
            if packet_type == "marks":
                for item in payload:
                    self.mark_price.emit(item)
                continue
            if packet_type in {"trades", "trade_batch"}:
                if not self.orderbook_streaming_enabled:
                    continue
                if payload:
                    self.trade_batch.emit(list(payload))
                continue
            if packet_type == "liquidations":
                for item in payload:
                    self.liquidation.emit(item)
                continue

            if packet_type == "depth":
                if isinstance(payload, dict):
                    self._dispatch_depth_payload(
                        generation, self.depth_revision, payload
                    )

            elif packet_type == "kline":
                row = payload.get("k") or {}
                event_time = safe_float(payload.get("E"))
                if (
                    payload.get("s") != self.symbol
                    or row.get("i") != self.interval
                    or event_time < self._kline_event_time
                ):
                    continue
                self._kline_event_time = event_time
                self._update_chart_cache(payload)
                self.kline.emit(payload)

            elif packet_type == "mark":
                self.mark_price.emit(payload)
            elif packet_type == "liquidation":
                self.liquidation.emit(payload)
            elif packet_type == "trade":
                if not self.orderbook_streaming_enabled:
                    continue
                if isinstance(payload, dict):
                    self.trade_batch.emit([payload])


    def _launch_read(self, function, finished, failed, *, source='market data') -> ApiTask:
        lifecycle = self._lifecycle
        task: ApiTask

        def current() -> bool:
            return lifecycle == self._lifecycle and not self.stopping

        def done(result: Any) -> None:
            self.tasks.discard(task)
            if current():
                finished(result)

        def error(message: str) -> None:
            self.tasks.discard(task)
            if current():
                failed(message)

        def execute():
            if not current():
                raise RuntimeError('Request superseded.')
            return function()

        background = source in {'analysis', 'interest_history', 'overview', 'chart prefetch'}
        pool = self._background_pool if background else self.task_pool
        task = launch_task(execute, done, error, pool,
                           defer_rate_waits=True, source=source, valid=current)
        return task

    @staticmethod
    def _chart_snapshot(payload: dict[str, Any]) -> dict[str, Any]:


        candles = payload.get("candles", ())
        return {**payload, "candles": candles.snapshot() if isinstance(candles, CandlePages) else candles}

    def _queue_chart_cache(self, key, payload, *, requested_at=0.0,
                           publish=False, generation=None, callback=None,
                           if_absent=False, timestamp=None, skip_newer=False):
        if self.stopping or not payload.get("candles"):
            return
        self._cache_prepare_queue.append({
            "key": key, "payload": payload, "requested_at": requested_at,
            "publish": publish, "generation": generation, "callback": callback,
            "if_absent": if_absent, "timestamp": timestamp, "skip_newer": skip_newer,
            "lifecycle": self._lifecycle,
        })
        self._start_cache_prepare()

    def _start_cache_prepare(self):
        if self._cache_prepare_active is not None or self.stopping:
            return
        while self._cache_prepare_queue:
            request = self._cache_prepare_queue.popleft()
            key = request["key"]
            cached = self.chart_cache.get(key)
            if request["lifecycle"] != self._lifecycle:
                continue
            if cached and (request["if_absent"] or (request["skip_newer"] and cached[0] >= request["requested_at"])):
                continue
            break
        else:
            return
        base = self._chart_snapshot(cached[1]) if cached else {}
        request["base"] = cached[1] if cached else None
        request["base_tail"] = base["candles"][-1] if base.get("candles") else None
        request["base_count"] = len(base.get("candles", ()))
        request["base_tail_rows"] = {c.time: c for c in base.get("candles", ())[-4:]}
        self._cache_prepare_active = request
        self._cache_prepare_serial += 1
        live = tuple(self._live_candles.values()) if key == (self.symbol, self.interval) else ()
        self._cache_prepare_job.submit(
            self._cache_prepare_serial, _prepare_chart_cache_job,
            base, request["payload"], live, request["requested_at"],
        )

    @QtCore.Slot(object, object)
    def _cache_prepared(self, _serial, payload):
        request, self._cache_prepare_active = self._cache_prepare_active, None
        if request is None or self.stopping or request["lifecycle"] != self._lifecycle:
            self._start_cache_prepare()
            return
        key = request["key"]
        cached = self.chart_cache.get(key)
        current = cached[1] if cached else None
        if current is not request["base"]:

            self._cache_prepare_queue.appendleft(request)
            self._start_cache_prepare()
            return


        extra = ()
        if current and current.get("candles") and current["candles"][-1] is not request["base_tail"]:
            rows = current["candles"]
            if (abs(len(rows) - request["base_count"]) > 4
                    or (len(rows) >= 4 and request["base_tail"] is not None
                        and rows[-4].time > request["base_tail"].time)):
                self._cache_prepare_queue.appendleft(request)
                self._start_cache_prepare()
                return
            extra = tuple((time.monotonic(), candle) for candle in rows[-4:]
                          if request["base_tail_rows"].get(candle.time) is not candle)
        live = tuple(self._live_candles.values()) if key == (self.symbol, self.interval) else ()
        if not self._patch_chart_tail(payload, (*extra, *live), request["requested_at"]):
            request["payload"] = payload
            self._cache_prepare_queue.appendleft(request)
            self._start_cache_prepare()
            return
        if current and current.get("oi_history"):
            payload["oi_history"] = current["oi_history"]
        self._remember(self.chart_cache, key, payload, self._chart_cache_capacity())
        if request["timestamp"] is not None:
            self.chart_cache[key] = (request["timestamp"], payload)
        active = (not self.suspended and key == (self.symbol, self.interval)
                  and request["generation"] == self.generation)
        if request["publish"] and active:
            self.bootstrap_ready.emit(self._chart_snapshot(payload))
        if request["callback"] is not None and active:
            request["callback"]()
        self._start_cache_prepare()

    @QtCore.Slot(object, str)
    def _cache_prepare_failed(self, _serial, message):
        request, self._cache_prepare_active = self._cache_prepare_active, None
        if request and not self.stopping and request["generation"] == self.generation:
            self.problem.emit(f"Chart history preparation failed: {message}")
            self._mark_chart_history_repair()
        self._start_cache_prepare()

    @staticmethod
    def _patch_chart_tail(payload, live, requested_at):
        candles = payload["candles"]
        latest = candles[-1].time if candles else 0
        for stamp, candle in sorted(live, key=lambda item: item[1].time):
            if candles and candle.time < candles[0].time:
                continue
            if stamp < requested_at and candle.time <= latest:
                continue
            index = bisect_left(candles, candle.time, key=lambda row: row.time)
            if index == len(candles):
                candles.append(candle)
                if len(candles) > MAX_CHART_CANDLES:
                    del candles[:len(candles) - MAX_CHART_CANDLES]
            elif candles[index].time == candle.time:
                candles[index] = candle
            else:

                return False
        return True

    def _run(
        self,
        function: Callable[[], Any],
        finished: Callable[[Any], None],
        generation: int | None = None,
        report_failure: bool = True,
        request_key: tuple[Any, ...] | None = None,
        stale_finished: Callable[[Any], None] | None = None,
        on_failure: Callable[[str], None] | None = None,
    ) -> None:
        if request_key is not None:
            if request_key in self._pending_reads:
                return
            self._pending_reads.add(request_key)
        task: ApiTask

        def done(result: Any) -> None:
            self.tasks.discard(task)
            self._pending_reads.discard(request_key)
            if not self.stopping and not self.suspended and (generation is None or generation == self.generation):
                finished(result)
            elif (
                stale_finished is not None
                and generation is not None
                and generation != self.generation
                and not self.stopping
            ):
                stale_finished(result)

        def failed(message: str) -> None:
            self.tasks.discard(task)
            self._pending_reads.discard(request_key)
            if on_failure is not None and not self.stopping and not self.suspended and (generation is None or generation == self.generation):
                on_failure(message)
            if (
                report_failure
                and not self.stopping
                and (generation is None or generation == self.generation)
            ):
                self.problem.emit(message)

        task = self._launch_read(function, done, failed, source=str(request_key[0]) if request_key else 'chart bootstrap/history')
        self.tasks.add(task)

    @staticmethod
    def _remember(
        cache: OrderedDict[Any, tuple[float, dict[str, Any]]],
        key: Any,
        payload: dict[str, Any],
        limit: int,
    ) -> None:
        cache[key] = (time.monotonic(), payload)
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)

    def _load_universe(self) -> None:
        """Load exchangeInfo and retry transient startup failures."""
        if self.stopping or self.suspended or self.universe_loaded:
            return

        def fetch() -> dict[str, Any]:
            try:
                return {"ok": True, "payload": self.rest.exchange_info()}
            except (RuntimeError, TimeoutError, OSError) as exc:
                return {"ok": False, "error": str(exc)}

        def loaded(result: dict[str, Any]) -> None:
            if self.stopping or self.suspended:
                return
            if result.get("ok"):
                self.universe_loaded = True
                self.universe_retry_timer.stop()
                self.universe_ready.emit(result.get("payload") or {})
                return
            message = str(result.get("error") or "Binance exchangeInfo unavailable")
            self.problem.emit(message)
            if not self.universe_retry_timer.isActive():
                self.universe_retry_timer.start()

        self._run(fetch, loaded, report_failure=False)

    def start(self, symbol: str, interval: str) -> None:
        self.stopping = False
        self.suspended = False
        self.started = True
        self._ensure_parser_thread()
        self.watchdog_timer.start()
        if self.chart_only:
            self.switch_market(symbol, interval)
            return

        self.universe_loaded = False
        self._load_universe()




        self.refresh_market_overview()

        if not self.ticker_symbols:
            self.ticker_symbols = (symbol.upper(),)
        self._open_ticker_socket()
        self.switch_market(symbol, interval)
        self._rebuild_chart_prefetch_queue()
        self.oi_timer.start()
        self.analysis_timer.start()
        self.overview_timer.start()
        if self.orderbook_streaming_enabled:
            self.depth_fallback_timer.start()

    def _chart_cache_capacity(self) -> int:


        return max(CHART_CACHE_LIMIT, len(self.chart_prefetch_symbols) + 4)

    def set_interaction_priority(self, active: bool) -> None:
        """Pause optional watchlist warming, never live/trading transport."""
        self._interaction_priority = bool(active)
        if active:
            self._chart_prefetch_timer.stop()
        else:
            self._rebuild_chart_prefetch_queue()

    def set_chart_prefetch_symbols(
        self,
        symbols: list[str] | tuple[str, ...],
    ) -> None:
        """Warm the current timeframe for explicit watchlist markets."""
        updated = tuple(
            dict.fromkeys(
                str(symbol).upper()
                for symbol in symbols
                if symbol
            )
        )
        if updated == self.chart_prefetch_symbols:
            return
        self.chart_prefetch_symbols = updated
        self._rebuild_chart_prefetch_queue()

    def _rebuild_chart_prefetch_queue(self) -> None:
        if self.chart_only:
            return

        interval = self.interval
        wanted = [
            (symbol, interval)
            for symbol in self.chart_prefetch_symbols
            if symbol != self.symbol
        ]
        active = self._chart_prefetch_active
        self._chart_prefetch_queue = [
            key
            for key in wanted
            if key != active and key not in self.chart_cache
        ]

        if (
            self.started
            and not self.stopping
            and not self.suspended
            and self._chart_prefetch_queue
            and not self._interaction_priority
            and self._chart_prefetch_active is None
            and not self._chart_prefetch_timer.isActive()
        ):

            self._chart_prefetch_timer.start()

    def _run_next_chart_prefetch(self) -> None:
        if (
            self.chart_only
            or self.stopping
            or self.suspended
            or self._interaction_priority
            or not self.started
            or self._chart_prefetch_active is not None
        ):
            return

        while self._chart_prefetch_queue:
            key = self._chart_prefetch_queue.pop(0)
            if key in self.chart_cache:
                continue
            symbol, interval = key
            if (
                symbol not in self.chart_prefetch_symbols
                or interval != self.interval
                or symbol == self.symbol
            ):
                continue
            break
        else:
            return

        self._chart_prefetch_active = key
        task: ApiTask

        def done(payload: dict[str, Any]) -> None:
            self.tasks.discard(task)
            if self._chart_prefetch_active == key:
                self._chart_prefetch_active = None

            if (
                not self.stopping
                and not self.suspended
                and payload.get("candles")
                and key[0] in self.chart_prefetch_symbols
            ):
                payload["oi_history"] = []
                self._queue_chart_cache(key, payload, if_absent=True)

            if (
                self.started
                and not self.stopping
                and not self.suspended
                and self._chart_prefetch_queue
            ):



                self._chart_prefetch_timer.start(350)

        def failed(_message: str) -> None:
            self.tasks.discard(task)
            if self._chart_prefetch_active == key:
                self._chart_prefetch_active = None
            if (
                self.started
                and not self.stopping
                and not self.suspended
                and self._chart_prefetch_queue
            ):
                self._chart_prefetch_timer.start(700)

        def fetch_prefetch() -> dict[str, Any]:
            if (
                self.stopping
                or self.suspended
                or key[1] != self.interval
                or key[0] == self.symbol
                or key[0] not in self.chart_prefetch_symbols
            ):
                return {"_superseded": True, "candles": []}
            payload = self.rest.candles_snapshot(
                key[0],
                key[1],
                priority="background",
            )
            self._persist_chart_payload(key[0], key[1], payload)
            return payload

        task = self._launch_read(
            fetch_prefetch, done, failed, source='chart prefetch',
        )
        self.tasks.add(task)

    def set_valid_ticker_symbols(self, symbols: object) -> None:
        """Install the validated exchange universe for worker-side ticker filtering."""
        self._valid_ticker_symbols = frozenset(
            str(symbol).upper() for symbol in (symbols or ()) if symbol
        )

    def set_ticker_symbols(self, symbols: list[str] | tuple[str, ...]) -> None:
        updated = tuple(sorted({symbol.upper() for symbol in symbols if symbol}))
        if updated == self.ticker_symbols:
            return
        self.ticker_symbols = updated

        if self.started and not self.stopping and self.ticker_socket is None:
            self._open_ticker_socket()


    def _load_persistent_chart_cache(
        self,
        symbol: str,
        interval: str,
    ) -> dict[str, Any] | None:
        """Load a bounded local snapshot for immediate first paint."""
        if self.db is None:
            return None
        seconds = int(INTERVAL_SECONDS.get(interval, 0))
        if seconds <= 0:
            return None
        end_ms = int(time.time() * 1000.0)
        start_ms = max(
            0,
            end_ms - int(MAX_CHART_CANDLES * seconds * 1000 * 1.15),
        )
        try:
            candles = self.db.load_candles(
                symbol,
                interval,
                start_ms,
                end_ms,
            )
        except Exception as exc:
            if self._diagnostics is not None:
                self._diagnostics.increment("market.chart_cache.read_errors")
                self._diagnostics.error("CHART CACHE", f"{symbol} {interval} read: {exc}")
            return None
        candles = list(candles[-MAX_CHART_CANDLES:])
        if len(candles) < 8:
            return None
        return {
            "symbol": symbol,
            "interval": interval,
            "candles": candles,
            "candle_matrix": _candle_matrix_from_objects(candles),
            "oi_history": [],
            "_persistent_cache": True,
        }

    def _persist_chart_payload(
        self,
        symbol: str,
        interval: str,
        payload: dict[str, Any],
    ) -> None:
        if self.db is None:
            return
        candles = payload.get("candles") or []
        if not candles:
            return
        try:
            self.db.cache_candles(symbol, interval, candles)
        except Exception as exc:
            if self._diagnostics is not None:
                self._diagnostics.increment("market.chart_cache.write_errors")
                self._diagnostics.error("CHART CACHE", f"{symbol} {interval} write: {exc}")

    def switch_market(self, symbol: str, interval: str) -> None:
        keep_connection = symbol.upper() == self.symbol and self._market_sockets_ready()
        self.symbol = symbol.upper()
        self.interval = interval
        selected_symbol = self.symbol
        selected_interval = self.interval
        self.generation += 1
        generation = self.generation
        self._deferred_analysis_request = None
        self._live_candles.clear()
        self._kline_event_time = 0.0
        self._chart_needs_repair = False
        self._chart_gap_start = None
        self._chart_repair_revision += 1
        self._chart_repair_attempts = 0
        self._chart_repair_timer.stop()
        self._retry_attempts["public"] = 0
        self._retry_attempts["market"] = 0
        self.public_reconnect_timer.stop()
        self.market_reconnect_timer.stop()
        if keep_connection:



            if not self.subscription_timer.isActive():
                self.subscription_timer.start()
        else:
            self.subscription_timer.stop()
            self._close_market_sockets()
            depth_epoch = (
                self._begin_depth_epoch(self.symbol)
                if not self.chart_only and self.orderbook_streaming_enabled
                else 0
            )
            self.status.emit("CONNECTING", False)
            self._open_market_sockets(generation)
            if depth_epoch:
                self._load_depth_snapshot(depth_epoch)

        key = (selected_symbol, selected_interval)
        if key in self._chart_prefetch_queue:
            self._chart_prefetch_queue = [
                queued for queued in self._chart_prefetch_queue if queued != key
            ]
        self._rebuild_chart_prefetch_queue()

        now = time.monotonic()
        cached = self.chart_cache.get(key)
        if cached is None:
            local_requested_at = time.monotonic()

            def local_loaded(local_payload: dict[str, Any] | None) -> None:


                if local_payload is None or key in self.chart_cache:
                    return
                self._queue_chart_cache(
                    key, local_payload, requested_at=local_requested_at,
                    publish=True, generation=generation, if_absent=True, timestamp=0.0,
                )



            self._run(
                lambda: self._load_persistent_chart_cache(
                    selected_symbol, selected_interval,
                ),
                local_loaded,
                generation,
                report_failure=False,
                request_key=("local_chart", generation),
            )
        if cached:
            self.chart_cache.move_to_end(key)
            self.bootstrap_ready.emit(self._chart_snapshot(cached[1]))
            if self.chart_only and now - cached[0] < CHART_CACHE_REFRESH_SECONDS:
                return
        analysis_cached = self.analysis_cache.get(selected_symbol)
        if analysis_cached and not self.chart_only:
            self.analysis_cache.move_to_end(selected_symbol)
            self.analysis_ready.emit({**analysis_cached[1], "interval": selected_interval})

        chart_stale = not cached or now - cached[0] >= CHART_CACHE_REFRESH_SECONDS
        analysis_stale = (
            not analysis_cached
            or now - analysis_cached[0] >= ANALYSIS_CACHE_REFRESH_SECONDS
        )
        if not chart_stale:
            if not cached[1].get("oi_history"):
                self._load_interest_history(generation, selected_symbol, selected_interval)
            if analysis_stale:
                self._request_analysis_when_book_ready(
                    generation, selected_symbol, selected_interval
                )
            return

        requested_at = time.monotonic()

        def after_loaded() -> None:
            if self.chart_only:
                return
            self._load_interest_history(generation, selected_symbol, selected_interval)
            if analysis_stale:
                self._request_analysis_when_book_ready(
                    generation, selected_symbol, selected_interval
                )

        def loaded(payload: dict[str, Any]) -> None:
            payload["oi_history"] = []
            self._queue_chart_cache(
                key, payload, requested_at=requested_at, publish=True,
                generation=generation, callback=after_loaded,
            )

        def stale_loaded(payload: dict[str, Any]) -> None:
            if not isinstance(payload, dict) or not payload.get("candles"):
                return
            cached_now = self.chart_cache.get(key)
            if cached_now is not None and cached_now[0] >= requested_at:



                return



            payload["oi_history"] = []
            self._queue_chart_cache(
                key, payload, requested_at=requested_at, skip_newer=True,
            )

        def fetch_chart_snapshot() -> dict[str, Any]:
            if generation != self.generation or self.stopping or self.suspended:
                return {"_superseded": True}
            payload = self.rest.candles_snapshot(
                selected_symbol,
                selected_interval,
            )
            self._persist_chart_payload(
                selected_symbol,
                selected_interval,
                payload,
            )
            return payload

        self._run(
            fetch_chart_snapshot,
            loaded,
            generation,
            stale_finished=stale_loaded,
            on_failure=lambda _message: self._mark_chart_history_repair(),
        )

    def _load_interest_history(self, generation: int, symbol: str, interval: str) -> None:
        key = (symbol, interval)

        def loaded(payload: dict[str, Any]) -> None:
            cached = self.chart_cache.get(key)
            if cached:
                cached[1]["oi_history"] = payload.get("oi_history", [])
            self.interest_history_ready.emit(payload)

        self._run(
            lambda: self.rest.interest_history(symbol, interval),
            loaded,
            generation,
            report_failure=False,
            request_key=("interest_history", generation),
        )

    def _request_analysis_when_book_ready(
        self,
        generation: int,
        symbol: str,
        interval: str,
    ) -> None:
        if self.chart_only or self.book_valid:
            cached = self.chart_cache.get((symbol, interval))
            candles = cached[1].get("candles", []) if cached else []
            self._load_analysis(generation, symbol, interval, candles)
            return
        self._deferred_analysis_request = (generation, symbol, interval)

    def _flush_deferred_analysis(self) -> None:
        pending = self._deferred_analysis_request
        self._deferred_analysis_request = None
        if pending is None or not self.book_valid:
            return
        generation, symbol, interval = pending
        if generation != self.generation or symbol != self.symbol:
            return
        cached = self.chart_cache.get((symbol, interval))
        candles = cached[1].get("candles", []) if cached else []
        self._load_analysis(generation, symbol, interval, candles)

    def _load_analysis(
        self,
        generation: int,
        symbol: str,
        interval: str,
        candles: list[Candle],
    ) -> None:
        candles = list(candles)[-800:]

        def loaded(payload: dict[str, Any]) -> None:
            self._remember(self.analysis_cache, symbol, payload, ANALYSIS_CACHE_LIMIT)
            self.analysis_ready.emit(payload)

        self._run(
            lambda: self.rest.analysis_snapshot(symbol, interval, list(candles[-800:])),
            loaded,
            generation,
            report_failure=False,
            request_key=("analysis", generation),
        )

    def refresh_analysis(self) -> None:
        key = (self.symbol, self.interval)
        cached = self.chart_cache.get(key)
        if cached:
            self._load_analysis(
                self.generation,
                self.symbol,
                self.interval,
                cached[1].get("candles", []),
            )

    def refresh_interest(self) -> None:
        generation = self.generation
        symbol = self.symbol

        def loaded(payload: dict[str, Any]) -> None:
            if generation == self.generation:
                self.interest.emit(payload)

        self._run(lambda: self.rest.current_interest(symbol), loaded, generation, report_failure=False,
                  request_key=("interest", generation))

    def refresh_market_overview(self) -> None:
        if self._ticker_live_at and time.monotonic() - self._ticker_live_at < 10:
            return
        self._run(
            self.rest.market_tickers,
            self.ticker_batch.emit,
            report_failure=False,
            request_key=("overview", 0),
        )

    def refresh_depth_fallback(self) -> None:
        if (
            self.chart_only
            or not self.orderbook_streaming_enabled
            or self.stopping
            or self.suspended
            or self.depth_epoch <= 0
            or not self.connected.get("public", False)
        ):
            return
        if self.book_valid:
            return
        if self.depth_snapshot_epoch == self.depth_epoch:
            return
        if self._depth_snapshot_seeded_epoch == self.depth_epoch:


            if time.monotonic() - self._depth_snapshot_seeded_at < 5.0:
                return
            self._invalidate_depth_book(
                "DEPTH SNAPSHOT NOT BRIDGED",
                freeze_worker=True,
                request_snapshot=True,
            )
            return
        self._load_depth_snapshot(self.depth_epoch)

    def _schedule_depth_snapshot_retry(
        self, epoch: int, revision: int, reason: str
    ) -> None:
        if (
            epoch != self.depth_epoch
            or revision != self.depth_revision
            or self.stopping
            or self.suspended
            or self.chart_only
            or not self.orderbook_streaming_enabled
        ):
            return
        if self._depth_snapshot_retry_epoch != epoch:
            self._depth_snapshot_retry_epoch = epoch
            self._depth_snapshot_retry_count = 0
        attempt = self._depth_snapshot_retry_count
        if attempt >= len(DEPTH_SNAPSHOT_RETRY_DELAYS_MS):
            return
        delay_ms = DEPTH_SNAPSHOT_RETRY_DELAYS_MS[attempt]
        self._depth_snapshot_retry_count = attempt + 1
        scheduled_attempt = self._depth_snapshot_retry_count

        def retry() -> None:
            if (
                epoch != self.depth_epoch
                or revision != self.depth_revision
                or self.stopping
                or self.suspended
                or self.chart_only
                or not self.orderbook_streaming_enabled
                or not self.connected.get("public", False)
                or self._depth_snapshot_retry_epoch != epoch
                or self._depth_snapshot_retry_count != scheduled_attempt
            ):
                return

            if self.last_depth_event > 0.0 and time.monotonic() - self.last_depth_event < 1.0:
                self._depth_snapshot_retry_epoch = None
                self._depth_snapshot_retry_count = 0
                return
            self._load_depth_snapshot(epoch, revision=revision)

        QTimer.singleShot(delay_ms, retry)

    def _load_depth_snapshot(
        self,
        epoch: int,
        *,
        revision: int | None = None,
        force: bool = False,
    ) -> None:
        revision = self.depth_revision if revision is None else int(revision)
        if (
            epoch != self.depth_epoch
            or revision != self.depth_revision
            or self.chart_only
            or not self.orderbook_streaming_enabled
            or not self.connected.get("public", False)
        ):
            return
        if self.depth_snapshot_epoch == epoch:
            if force:
                self._depth_force_refresh_epoch = epoch
            return
        estimated_rate_wait = BINANCE_RATE_LIMITER.estimated_wait(20, priority='repair')
        if estimated_rate_wait >= 0.05:
            prior_reason = str(self._book_invalid_reason or '').strip().upper()
            if prior_reason and prior_reason not in {'READY', 'SYNCING', 'ORDER BOOK INVALID'}:
                rate_reason = f'{prior_reason} · RESYNC RATE LIMITED'
            else:
                rate_reason = 'DEPTH RESYNC RATE LIMITED'
            self._set_book_valid(False, rate_reason)
        self.depth_snapshot_epoch = epoch
        if force:
            self._depth_force_refresh_epoch = None
        symbol = self.symbol

        def fetch() -> dict[str, Any]:
            rest_started = time.perf_counter()
            try:
                payload = self.rest.get(
                    "/fapi/v1/depth",
                    {"symbol": symbol, "limit": 1000},
                )
            except Exception as exc:  # noqa: BLE001 - bounded retry below
                return {"_snapshot_error": f"{type(exc).__name__}: {exc}"}
            if not isinstance(payload, dict):
                return {"_snapshot_error": "non-dictionary response"}
            try:
                snapshot_id = int(payload.get("lastUpdateId") or 0)
            except (TypeError, ValueError, OverflowError):
                return {"_snapshot_error": "invalid lastUpdateId"}
            return {
                "s": symbol,
                "lastUpdateId": snapshot_id,
                "E": payload.get("E", 0),
                "T": payload.get("T", 0),
                "b": payload.get("bids", []),
                "a": payload.get("asks", []),
            }

        def loaded(payload: dict[str, Any]) -> None:
            if self.depth_snapshot_epoch == epoch:
                self.depth_snapshot_epoch = None

            if self._depth_force_refresh_epoch == epoch:



                self._depth_force_refresh_epoch = None
                if (
                    epoch == self.depth_epoch
                    and not self.stopping
                    and not self.suspended
                    and self.orderbook_streaming_enabled
                ):
                    QTimer.singleShot(
                        0,
                        lambda: self._load_depth_snapshot(
                            epoch, revision=self.depth_revision
                        ),
                    )
                return

            if epoch != self.depth_epoch or revision != self.depth_revision:
                return
            snapshot_bids = _clean_depth_rows(payload.get("b", []), reverse=True)
            snapshot_asks = _clean_depth_rows(payload.get("a", []), reverse=False)
            invalid_reason = str(payload.get("_snapshot_error") or "")
            if not payload.get("lastUpdateId"):
                invalid_reason = invalid_reason or "invalid lastUpdateId"
            elif not snapshot_bids or not snapshot_asks:
                invalid_reason = invalid_reason or "invalid/empty snapshot"
            elif snapshot_bids[0][0] >= snapshot_asks[0][0]:
                invalid_reason = invalid_reason or "crossed snapshot"
            if invalid_reason:
                self._set_book_valid(False, f"SNAPSHOT: {invalid_reason}")
                self._depth_snapshot_seeded_epoch = None
                self._depth_snapshot_seeded_at = 0.0
                self._schedule_depth_snapshot_retry(
                    epoch, revision, invalid_reason
                )
                return
            self._depth_snapshot_seeded_epoch = epoch
            self._depth_snapshot_seeded_at = time.monotonic()



            if self._depth_snapshot_retry_epoch == epoch:
                self._depth_snapshot_retry_epoch = None
                self._depth_snapshot_retry_count = 0
            if self._depth_parser_worker is not None:
                self._depth_seed_request.emit(epoch, revision, payload)
                return




            self._set_book_valid(False, "DEPTH WORKER UNAVAILABLE")
            self.problem.emit("Order book unavailable: depth sequence worker is not running.")

        self._run(
            fetch,
            loaded,
            report_failure=False,
            request_key=("depth_snapshot", epoch, revision),
        )

    def inject_history(self, symbol: str, interval: str, candles: list[Candle]) -> None:
        self._queue_chart_cache(
            (symbol, interval), {"symbol": symbol, "interval": interval, "candles": candles},
        )

    def _open_public_socket(self, generation: int) -> None:
        if (
            self.stopping
            or self.suspended
            or self.chart_only
            or not self.orderbook_streaming_enabled
        ):
            return
        symbol = self.symbol.lower()
        public_streams = "/".join(
            (
                f"{symbol}@depth@100ms",
                f"{symbol}@bookTicker",
            )
        )
        self.public_socket = self._make_socket(
            "public",
            f"{self.ws_base}/public/stream?streams={public_streams}",
            generation,
        )

    def _open_market_socket(self, generation: int) -> None:
        if self.stopping or self.suspended:
            return
        self._subscribed_interval = self.interval
        symbol = self.symbol.lower()
        if self.chart_only:
            market_streams = f"{symbol}@kline_{self.interval}"
            self._market_socket_has_agg_trade = False
        else:
            self.market_epoch += 1
            streams = [
                f"{symbol}@kline_{self.interval}",
                "!forceOrder@arr",
                f"{symbol}@markPrice@1s",
            ]
            if self.orderbook_streaming_enabled:
                streams.insert(1, f"{symbol}@aggTrade")
            market_streams = "/".join(streams)
            self._market_socket_has_agg_trade = self.orderbook_streaming_enabled
        self.market_socket = self._make_socket(
            "market",
            f"{self.ws_base}/market/stream?streams={market_streams}",
            generation,
        )

    def _open_market_sockets(self, generation: int) -> None:
        if self.stopping or self.suspended:
            return
        if not self.chart_only and self.orderbook_streaming_enabled:
            self._open_public_socket(generation)
        self._open_market_socket(generation)

    def _open_ticker_socket(self) -> None:
        if self.stopping or self.suspended:
            return
        streams = "!miniTicker@arr/!markPrice@arr@1s"
        self.ticker_socket = self._make_socket(
            "ticker",
            f"{self.ws_base}/market/stream?streams={streams}",
            self.generation,
        )

    def _resubscribe_kline(self) -> None:
        if (self.stopping or self.suspended or not self.market_socket
                or not self.connected["market"] or self._subscribed_interval == self.interval):
            return
        symbol = self.symbol.lower()
        for offset, method, interval in ((0, "UNSUBSCRIBE", self._subscribed_interval),
                                         (1, "SUBSCRIBE", self.interval)):
            self.market_socket.sendTextMessage(json.dumps({"method": method,
                "params": [f"{symbol}@kline_{interval}"], "id": self.generation*2+offset}))
        self._subscribed_interval = self.interval

    def _sync_agg_trade_subscription(self) -> None:
        """Match the shared market socket's aggTrade subscription to DOM demand."""
        if self.chart_only:
            return
        desired = bool(self.orderbook_streaming_enabled)
        if desired == self._market_socket_has_agg_trade:
            self._publish_trade_stream_status()
            return
        socket = self.market_socket
        if (
            socket is None
            or not self.connected.get("market", False)
            or socket.state() != QtNetwork.QAbstractSocket.SocketState.ConnectedState
        ):
            if desired:
                self._publish_trade_stream_status(False, 'MARKET STREAM OFFLINE')
            else:
                self._publish_trade_stream_status(False, 'PAUSED')
            return
        method = "SUBSCRIBE" if desired else "UNSUBSCRIBE"
        message_id = self.generation * 10 + (7 if desired else 8)
        try:
            queued = socket.sendTextMessage(
                json.dumps(
                    {
                        "method": method,
                        "params": [f"{self.symbol.lower()}@aggTrade"],
                        "id": message_id,
                    }
                )
            )
        except RuntimeError:
            return
        if queued >= 0:
            self._market_socket_has_agg_trade = desired
            self._publish_trade_stream_status()

    def _make_socket(self, kind: str, url: str, generation: int) -> QWebSocket:
        socket = QWebSocket()
        self._socket_generations[kind] = generation
        self._disconnect_tokens.pop(kind, None)
        def current():
            return socket is getattr(self, f"{kind}_socket", None) and self._socket_generations.get(kind) == generation


        socket.connected.connect(lambda: self._socket_connected(kind, generation) if current() else None)
        socket.disconnected.connect(
            lambda: self._socket_disconnected(kind, generation, socket)
            if current() else None
        )
        socket.textMessageReceived.connect(
            lambda message: self._queue_socket_message(
                kind,
                generation,
                message,
            ) if current() else None
        )
        socket.errorOccurred.connect(lambda _error: self._socket_error(kind, generation, socket.errorString()) if current() else None)
        socket.pong.connect(lambda *_args: self._socket_activity.update({kind: time.monotonic()}) if current() else None)

        def open_when_allowed() -> None:
            current_socket = {
                "public": self.public_socket,
                "market": self.market_socket,
                "ticker": self.ticker_socket,
            }.get(kind)
            if (
                self.stopping
                or self.suspended
                or current_socket is not socket



                or self._socket_generations.get(kind) != generation
            ):
                return
            delay = BINANCE_RATE_LIMITER.reserve_websocket_connection(api=False)
            if delay > 0:
                QTimer.singleShot(min(60_000, max(250, int(delay * 1000))), open_when_allowed)
                return
            started_at = time.monotonic()
            self._socket_started[kind] = started_at
            self._socket_activity[kind] = started_at
            self._socket_data_activity[kind] = started_at
            self._socket_healthy_since[kind] = started_at
            socket.open(QUrl(url))

        QTimer.singleShot(0, open_when_allowed)
        return socket

    def _socket_connected(self, kind: str, generation: int) -> None:
        self._disconnect_tokens.pop(kind, None)
        connected_at = time.monotonic()
        self._socket_activity[kind] = connected_at
        self._socket_data_activity[kind] = connected_at
        self._socket_healthy_since[kind] = connected_at
        if kind == "ticker":
            return
        if generation != self._socket_generations.get(kind):
            return
        self.connected[kind] = True
        if kind == "market" and not self.chart_only:
            self._sync_agg_trade_subscription()
            self._publish_trade_stream_status()
        if (
            kind == "public"
            and not self.chart_only
            and self.depth_epoch > 0
            and self._depth_snapshot_seeded_epoch != self.depth_epoch
            and self.depth_snapshot_epoch != self.depth_epoch
        ):
            QTimer.singleShot(0, lambda epoch=self.depth_epoch: self._load_depth_snapshot(epoch))
        if self._market_sockets_ready():
            self.status.emit("LIVE", True)
            if not self.chart_only and not self.book_valid:



                self.book_validity.emit(False, self._book_invalid_reason)
            if self._chart_needs_repair:
                self._repair_chart_history()
        else:
            self.status.emit("CONNECTING", False)

    def _socket_disconnected(
        self,
        kind: str,
        generation: int,
        socket: QWebSocket | None = None,
    ) -> None:
        if socket is not None:
            token = id(socket)
            if self._disconnect_tokens.get(kind) == token:
                return
            self._disconnect_tokens[kind] = token
        if kind == "ticker":
            if (
                not self.stopping
                and not self.suspended
                and not self.ticker_reconnect_timer.isActive()
            ):
                self._schedule_reconnect("ticker")
            return
        if generation != self._socket_generations.get(kind):
            return
        self.connected[kind] = False
        if kind == 'market' and not self.chart_only:
            self._publish_trade_stream_status(False, 'MARKET STREAM OFFLINE')
        if kind == "public" and not self.chart_only:
            if not self.orderbook_streaming_enabled:
                return
            self._invalidate_depth_book(
                "PUBLIC STREAM OFFLINE", freeze_worker=True, request_snapshot=False
            )
        if self.stopping or self.suspended:
            return
        self.status.emit("RECONNECTING", False)
        if kind == "market":
            self._chart_needs_repair = True
        timer = self.public_reconnect_timer if kind == "public" else self.market_reconnect_timer
        if not timer.isActive():
            self._schedule_reconnect(kind)

    def _socket_error(self, kind: str, generation: int, message: str) -> None:
        if generation != self._socket_generations.get(kind):
            return
        if self._diagnostics is not None:
            self._diagnostics.increment(f"market.transport.{kind}.errors")
            self._diagnostics.error("MARKET TRANSPORT", f"{kind}: {message or 'socket error'}")
        socket = {
            "public": self.public_socket,
            "market": self.market_socket,
            "ticker": self.ticker_socket,
        }.get(kind)
        if self.stopping or self.suspended:
            return
        if kind == "public" and not self.orderbook_streaming_enabled:
            return

        if kind == "ticker":



            if socket is not None and socket.state() != QtNetwork.QAbstractSocket.SocketState.UnconnectedState:
                socket.abort()
            self._socket_disconnected(kind, generation, socket)
            return

        if generation != self._socket_generations.get(kind):
            return
        self.connected[kind] = False
        if message:
            self.status.emit(f"{kind.upper()} OFFLINE", False)


        if kind == "market":
            self._chart_needs_repair = True



        if socket is not None and socket.state() != QtNetwork.QAbstractSocket.SocketState.UnconnectedState:
            socket.abort()
        self._socket_disconnected(kind, generation, socket)

    def _schedule_reconnect(self, kind: str) -> None:
        timer = {
            "public": self.public_reconnect_timer,
            "market": self.market_reconnect_timer,
            "ticker": self.ticker_reconnect_timer,
        }[kind]
        attempt = self._retry_attempts[kind]
        self._retry_attempts[kind] = min(attempt + 1, 5)
        timer.start(int(min(30_000, 1000 * 2 ** attempt) + random.uniform(0, 500)))

    def _check_sockets(self) -> None:
        if self.stopping or self.suspended:
            return
        now = time.monotonic()
        self._check_depth_progress_health(now)
        for kind in ("public", "market", "ticker"):
            socket = getattr(self, f"{kind}_socket", None)
            if socket is None or kind not in self._socket_started:
                continue
            age = now - self._socket_started[kind]
            data_age = now - self._socket_data_activity.get(kind, self._socket_started[kind])
            stale_threshold = {'public': PUBLIC_SOCKET_DATA_STALE_SECONDS, 'market': MARKET_SOCKET_DATA_STALE_SECONDS, 'ticker': TICKER_SOCKET_DATA_STALE_SECONDS}.get(kind, PUBLIC_SOCKET_DATA_STALE_SECONDS)
            stale_data = data_age > stale_threshold
            connecting = socket.state() == QtNetwork.QAbstractSocket.SocketState.ConnectingState
            if age >= 85_800 or stale_data or (connecting and age > 20):
                if kind == "public" and not self.chart_only:
                    self._set_book_valid(False, "PUBLIC DATA STALE")
                socket.abort()
                self._socket_disconnected(kind, self._socket_generations.get(kind, -1), socket)
                continue

            if socket.state() == QtNetwork.QAbstractSocket.SocketState.ConnectedState:


                healthy_for = now - self._socket_healthy_since.get(kind, now)
                if healthy_for >= 30.0 and data_age <= 5.0:
                    self._retry_attempts[kind] = 0
                if now - self._socket_ping_at.get(kind, 0.0) >= 20.0:
                    self._socket_ping_at[kind] = now
                    socket.ping(b"nw")

    def _mark_chart_history_repair(self, start_time: float | None = None) -> None:
        self._chart_needs_repair = True
        self._chart_repair_revision += 1
        if start_time is not None:
            self._chart_gap_start = min(self._chart_gap_start, start_time) if self._chart_gap_start is not None else start_time
        if not self._chart_repair_timer.isActive() and not self.stopping and not self.suspended:
            delay = min(30000, 1500 * (2 ** min(self._chart_repair_attempts, 5)))
            self._chart_repair_timer.start(delay)

    def _repair_chart_history(self):
        if not self._chart_needs_repair or self.stopping or self.suspended or not self.connected.get("market", False):
            return
        generation, symbol, interval = self.generation, self.symbol, self.interval
        revision = self._chart_repair_revision
        requested_at = time.monotonic()
        gap_start = self._chart_gap_start
        cached_rows = (self.chart_cache.get((symbol, interval), (0.0, {}))[1]).get("candles", [])
        if gap_start is None and cached_rows:
            gap_start = shift_candle_time(cached_rows[-1].time, interval)
        def after_repair():
            if revision == self._chart_repair_revision:
                self._chart_needs_repair = False
                self._chart_gap_start = None
                self._chart_repair_attempts = 0
                self._chart_repair_timer.stop()
            else:
                self._mark_chart_history_repair()

        def loaded(payload):
            if not payload.get("candles"):
                self._chart_repair_attempts += 1
                self._mark_chart_history_repair(gap_start)
                return
            self._queue_chart_cache(
                (symbol, interval), payload, requested_at=requested_at,
                publish=True, generation=generation, callback=after_repair,
            )

        def fetch_repair() -> dict[str, Any]:
            if generation != self.generation or self.stopping or self.suspended:
                return {"_superseded": True, "candles": []}
            payload = self.rest.candles_snapshot(symbol, interval, priority='repair')
            rows = payload.get("candles", [])
            if gap_start is not None and rows and gap_start < rows[0].time:
                start = max(gap_start, shift_candle_time(rows[-1].time, interval, -(MAX_CHART_CANDLES - 1)))
                missing = self.rest.candle_range(symbol, interval, int(start * 1000), int(rows[0].time * 1000) - 1)
                if not missing or missing[0].time > start:
                    raise RuntimeError("Chart backfill did not reach the missing interval.")
                combined = {c.time: c for c in missing}
                combined.update({c.time: c for c in rows})
                rows = sorted(combined.values(), key=lambda c: c.time)[-MAX_CHART_CANDLES:]
                payload["candles"] = rows
                payload["candle_matrix"] = _candle_matrix_from_objects(rows)
            if rows:
                if interval == "1M":
                    has_gap = any(
                        current.time > shift_candle_time(previous.time, interval)
                        for previous, current in zip(rows, rows[1:])
                    )
                else:
                    times = np.fromiter((c.time for c in rows), dtype=np.float64, count=len(rows))
                    has_gap = bool(np.any(np.diff(times) > INTERVAL_SECONDS[interval]))
                if has_gap:
                    raise RuntimeError("Chart backfill still contains a missing interval.")
            self._persist_chart_payload(symbol, interval, payload)
            return payload

        def failed(_message: str) -> None:
            self._chart_repair_attempts += 1
            self._mark_chart_history_repair(gap_start)

        self._run(fetch_repair, loaded, generation,
                  report_failure=False, request_key=("repair", generation), on_failure=failed)

    def _reconnect_leg(self, kind: str) -> None:
        if self.stopping or self.suspended:
            return
        if kind == "public":
            if (
                self.chart_only
                or not self.orderbook_streaming_enabled
                or self.connected.get("public", False)
            ):
                return
            self._close_primary_socket("public")
            epoch = self._begin_depth_epoch(self.symbol)
            self._open_public_socket(self.generation)
            self._load_depth_snapshot(epoch)
            return
        if kind == "market":
            if self.connected.get("market", False):
                return
            self._close_primary_socket("market")
            self._open_market_socket(self.generation)

    def _market_sockets_ready(self) -> bool:
        return self.connected["market"] and (
            self.chart_only
            or not self.orderbook_streaming_enabled
            or self.connected["public"]
        )

    def _reconnect_tickers(self) -> None:
        if not self.stopping and not self.suspended:
            self._close_ticker_socket()
            self._open_ticker_socket()


    def _update_chart_cache(self, event: dict[str, Any]) -> None:
        row = event.get("k") or {}
        if event.get("s") != self.symbol or row.get("i") != self.interval:
            return
        candle = Candle.from_stream(row)
        self._live_candles[candle.time] = (time.monotonic(), candle)
        for old in sorted(self._live_candles)[:-4]:
            del self._live_candles[old]
        key = (event.get("s", ""), row.get("i", self.interval))
        cached = self.chart_cache.get(key)
        if not cached:
            return
        payload = cached[1]
        candles = payload.get("candles")
        if not isinstance(candles, CandlePages):


            return
        if candles and candle.time < candles[-1].time:
            return
        if not candles or candles[-1].time < candle.time:
            if candles:
                expected_open = shift_candle_time(candles[-1].time, self.interval)
                if candle.time > expected_open:
                    self._mark_chart_history_repair(expected_open)
            candles.append(candle)
            if len(candles) > MAX_CHART_CANDLES:
                del candles[:len(candles) - MAX_CHART_CANDLES]
        else:
            candles[-1] = candle

        self.chart_cache[key] = (time.monotonic(), cached[1])
        self.chart_cache.move_to_end(key)

    def _close_primary_socket(self, kind: str) -> None:
        if kind not in {"public", "market"}:
            return
        socket = getattr(self, f"{kind}_socket", None)
        self.connected[kind] = False
        if kind == "market":
            self._market_socket_has_agg_trade = False
            self._publish_trade_stream_status(False, 'MARKET STREAM OFFLINE' if self.orderbook_streaming_enabled else 'PAUSED')
        if socket is not None:
            socket.blockSignals(True)
            socket.close()
            socket.deleteLater()
        setattr(self, f"{kind}_socket", None)
        self._socket_ping_at.pop(kind, None)

    def _close_market_sockets(self) -> None:
        self.subscription_timer.stop()
        if not self.chart_only:
            self._set_book_valid(False, "STREAM CLOSED")
        self._close_primary_socket("public")
        self._close_primary_socket("market")

    def _close_ticker_socket(self) -> None:
        if self.ticker_socket is not None:
            self.ticker_socket.blockSignals(True)
            self.ticker_socket.close()
            self.ticker_socket.deleteLater()
        self.ticker_socket = None
        self._socket_ping_at.pop('ticker', None)

    def set_orderbook_depth_capacity(self, limit_per_side: int) -> None:
        """Bound deep DOM publication without shrinking the canonical local book."""
        try:
            resolved = int(limit_per_side)
        except (TypeError, ValueError, OverflowError):
            resolved = _DepthParserWorker.PUBLISH_LEVELS
        resolved = max(
            _DepthParserWorker.PUBLISH_LEVELS,
            min(resolved, _DepthParserWorker.ANALYSIS_LEVELS),
        )
        if resolved == self._orderbook_depth_capacity:
            return
        self._orderbook_depth_capacity = resolved
        if self._depth_parser_worker is not None:
            self._depth_capacity_request.emit(resolved)

    def set_orderbook_streaming_enabled(self, enabled: bool) -> None:
        """Start/stop every network stream used exclusively by Market Depth."""
        if self.chart_only:
            return
        enabled = bool(enabled)
        if enabled == self.orderbook_streaming_enabled:

            if enabled and self.started and not self.stopping and not self.suspended:
                self._sync_agg_trade_subscription()
                if self.public_socket is None:
                    epoch = self._begin_depth_epoch(self.symbol)
                    self._open_public_socket(self.generation)
                    self._load_depth_snapshot(epoch)
                    self.depth_fallback_timer.start()
            return



        self.market_epoch += 1
        self.orderbook_streaming_enabled = enabled
        self._publish_trade_stream_status(False, 'TRADE STREAM CONNECTING' if enabled else 'PAUSED')

        if not enabled:
            self.public_reconnect_timer.stop()
            self.depth_fallback_timer.stop()
            if self.depth_epoch > 0:
                self._invalidate_depth_book(
                    "PAUSED",
                    freeze_worker=True,
                    request_snapshot=False,
                )
            else:
                self._set_book_valid(False, "PAUSED")
            self._close_primary_socket("public")
            self._sync_agg_trade_subscription()
            return

        if self.stopping or self.suspended or not self.started:
            self._set_book_valid(False, "PAUSED")
            return

        self._sync_agg_trade_subscription()
        self.depth_fallback_timer.start()
        self._close_primary_socket("public")
        epoch = self._begin_depth_epoch(self.symbol)
        self._open_public_socket(self.generation)
        self._load_depth_snapshot(epoch)

    def stop(self) -> None:
        self.watchdog_timer.stop()
        self.stopping = True
        self._cache_prepare_job.invalidate()
        self._cache_prepare_queue.clear()
        self._cache_prepare_active = None
        self._lifecycle += 1
        self.generation += 1
        self.market_epoch += 1
        self.depth_epoch += 1
        self._pending_reads.clear()
        self.suspended = False
        self.started = False
        self.oi_timer.stop()
        self.analysis_timer.stop()
        self.overview_timer.stop()
        self.depth_fallback_timer.stop()
        self.public_reconnect_timer.stop()
        self.market_reconnect_timer.stop()
        self.ticker_reconnect_timer.stop()
        self.universe_retry_timer.stop()
        self._chart_prefetch_timer.stop()
        self._chart_prefetch_queue.clear()
        self._chart_prefetch_active = None
        self._close_market_sockets()
        self._close_ticker_socket()
        self._stop_parser_thread()

    def suspend_for_history(self) -> None:
        """Pause public live traffic while the modal research download owns bandwidth."""
        if self.stopping or not self.started or self.suspended:
            return
        self.suspended = True
        self.watchdog_timer.stop()
        self.oi_timer.stop()
        self.analysis_timer.stop()
        self.overview_timer.stop()
        self.depth_fallback_timer.stop()
        self.public_reconnect_timer.stop()
        self.market_reconnect_timer.stop()
        self.ticker_reconnect_timer.stop()
        self.universe_retry_timer.stop()
        self._chart_prefetch_timer.stop()
        self._close_market_sockets()
        self._close_ticker_socket()
        self.status.emit("HISTORY DOWNLOAD", False)

    def resume_after_history(self) -> None:
        if self.stopping or not self.started or not self.suspended:
            return
        self.suspended = False
        self.watchdog_timer.start()
        self.generation += 1
        generation = self.generation
        depth_epoch = (
            self._begin_depth_epoch(self.symbol)
            if not self.chart_only and self.orderbook_streaming_enabled
            else 0
        )
        self.status.emit("CONNECTING", False)
        self._open_market_sockets(generation)
        if self.chart_only:
            return
        self._open_ticker_socket()
        if depth_epoch:
            self._load_depth_snapshot(depth_epoch)
        if not self.universe_loaded:
            self._load_universe()
        self.refresh_market_overview()
        self.oi_timer.start()
        self.analysis_timer.start()
        self.overview_timer.start()
        if self.orderbook_streaming_enabled:
            self.depth_fallback_timer.start()
        self._rebuild_chart_prefetch_queue()

    def set_market_data_api_key(self, api_key: str) -> None:
        changed = self.rest.market_data_api_key != str(api_key or "").strip()
        self.rest.set_market_data_api_key(api_key)
        if changed:
            self.analysis_cache.clear()
            if not self.chart_only and not self.suspended and self.chart_cache:
                QTimer.singleShot(0, self.refresh_analysis)
