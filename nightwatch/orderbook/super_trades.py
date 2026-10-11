"""Bounded, worker-owned detection of exceptional prints and execution sweeps."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import math
import uuid

from ..models import OrderFlowTradePrint


SUPER_TRADE_MULTIPLIER = 10.0
SUPER_TRADE_CAPACITY = 512
BURST_GAP_MS = 200
BURST_DURATION_MS = 1000


@dataclass(frozen=True, slots=True)
class SuperTradeEvent:
    sequence: int
    start_time_ms: int
    event_time_ms: int
    aggressor_side: str
    quantity: float
    notional: float
    price: float
    low_price: float
    high_price: float
    print_count: int
    tape_threshold: float


@dataclass(frozen=True, slots=True)
class SuperTradeFrame:
    symbol: str
    stream: str
    revision: int
    events: tuple[SuperTradeEvent, ...]


@dataclass(slots=True)
class _ExecutionBurst:
    sequence: int
    start_time_ms: int
    event_time_ms: int
    aggressor_side: str
    quantity: float = 0.0
    notional: float = 0.0
    low_price: float = math.inf
    high_price: float = 0.0
    last_price: float = 0.0
    print_count: int = 0
    tape_threshold: float = 0.0


class SuperTradeDetector:
    """Infer short same-side sweeps; public prints cannot identify accounts.

    Qualify against the tape threshold at arrival, before that print enters the
    adaptive sample. A qualifying burst has one stable ID and is updated in
    place, so its constituent prints never produce duplicate chart markers.
    """

    def __init__(self, symbol: str):
        self.symbol = symbol.upper()
        self.stream = uuid.uuid4().hex
        self.revision = 0
        self._sequence = 0
        self._burst: _ExecutionBurst | None = None
        self._events: OrderedDict[int, SuperTradeEvent] = OrderedDict()
        self._frame: SuperTradeFrame | None = None

    def reset(self, symbol: str) -> None:
        symbol = symbol.upper()
        if symbol != self.symbol:
            self.symbol = symbol
            self.stream = uuid.uuid4().hex
            self._events.clear()
            self._sequence = 0
        # Book/transport gaps always end inference, even on the same market.
        self._burst = None
        self.revision += 1
        self._frame = None

    def record(self, trade: OrderFlowTradePrint, threshold: float, tick_size: float) -> None:
        if (trade.event_time_ms <= 0 or trade.aggressor_side not in {"buy", "sell"}
                or not all(math.isfinite(value) and value > 0.0
                           for value in (trade.price, trade.quantity, trade.notional, threshold))):
            return
        burst = self._burst
        tolerance = max(tick_size, trade.price * 1e-10)
        continues = bool(
            burst is not None
            and trade.aggressor_side == burst.aggressor_side
            and 0 <= trade.event_time_ms - burst.event_time_ms <= BURST_GAP_MS
            and trade.event_time_ms - burst.start_time_ms <= BURST_DURATION_MS
            and (trade.price >= burst.last_price - tolerance if trade.aggressor_side == "buy"
                 else trade.price <= burst.last_price + tolerance)
        )
        if not continues:
            self._sequence += 1
            burst = self._burst = _ExecutionBurst(
                self._sequence, trade.event_time_ms, trade.event_time_ms, trade.aggressor_side)
        burst.event_time_ms = trade.event_time_ms
        burst.quantity += trade.quantity
        burst.notional += trade.notional
        burst.low_price = min(burst.low_price, trade.price)
        burst.high_price = max(burst.high_price, trade.price)
        burst.last_price = trade.price
        burst.print_count += 1
        if not burst.tape_threshold:
            if burst.notional < SUPER_TRADE_MULTIPLIER * threshold:
                return
            burst.tape_threshold = threshold
        self._events[burst.sequence] = SuperTradeEvent(
            burst.sequence, burst.start_time_ms, burst.event_time_ms, burst.aggressor_side,
            burst.quantity, burst.notional, burst.notional / burst.quantity,
            burst.low_price, burst.high_price, burst.print_count, burst.tape_threshold)
        while len(self._events) > SUPER_TRADE_CAPACITY:
            self._events.popitem(last=False)
        self.revision += 1
        self._frame = None

    def frame(self) -> SuperTradeFrame:
        if self._frame is None:
            self._frame = SuperTradeFrame(self.symbol, self.stream, self.revision,
                                         tuple(self._events.values()))
        return self._frame
