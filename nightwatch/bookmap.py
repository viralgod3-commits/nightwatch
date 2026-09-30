


"""Temporal liquidity heatmap model and PyQtGraph companion surface.

The Bookmap is deliberately a sibling of the QPainter DOM, not a replacement.
It consumes the same validated full-depth publication that feeds Nightwatch's
order-flow analyzer, retains a bounded time×price history, and renders that
history with a PyQtGraph ImageItem.  The model is Qt-free so its lifecycle,
clock alignment, ring-buffer behavior, recentering, and stale-gap semantics can
be validated independently of the GUI runtime.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import numpy as np
BOOKMAP_CAPTURE_INTERVAL_MS = 100
BOOKMAP_RENDER_INTERVAL_MS = 100
BOOKMAP_HISTORY_MS = 180_000
BOOKMAP_DEFAULT_VISIBLE_MS = 60_000
BOOKMAP_PRICE_ROWS = 768
BOOKMAP_STALE_TIMEOUT_MS = 350
BOOKMAP_STALE_TIMEOUT_MIN_MS = 300
BOOKMAP_STALE_TIMEOUT_MAX_MS = 1_000
BOOKMAP_CLOCK_SAFETY_LAG_MS = 200
BOOKMAP_NORMALIZATION_FLOOR_PERCENTILE = 55.0
BOOKMAP_NORMALIZATION_PERCENTILE = 98.5
BOOKMAP_NORMALIZATION_GAMMA = 1.25
BOOKMAP_NORMALIZATION_HYSTERESIS = 0.10
BOOKMAP_MAX_TRADE_MARKERS = 256
BOOKMAP_TRADE_BIN_CAPACITY = 8192
BOOKMAP_PENDING_TRADE_CAPACITY = 4096
BOOKMAP_GUARD_FRACTION = 0.20
BOOKMAP_TARGET_COVERAGE_FRACTION = 0.78


@dataclass(frozen=True, slots=True)
class BookmapRenderFrame:
    """One immutable renderer handoff assembled from the bounded ring buffer."""

    revision: int
    symbol: str
    image: np.ndarray
    validity: np.ndarray
    oldest_time_s: float
    newest_time_s: float
    price_origin: float
    price_step: float
    best_bid: float
    best_ask: float
    microprice: float
    normalization_low: float
    normalization_high: float
    live: bool
    book_valid: bool
    validity_reason: str


@dataclass(frozen=True, slots=True)
class BookmapTradePoints:
    x: np.ndarray
    y: np.ndarray
    size: np.ndarray
    total: np.ndarray
    imbalance: np.ndarray
    rpi: np.ndarray
    rpi_fraction: np.ndarray


def _finite_positive(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if math.isfinite(number) and number > 0.0 else 0.0




def _format_bookmap_price(value: float, step: float = 0.0) -> str:
    value = float(value)
    step = _finite_positive(step)
    if not math.isfinite(value):
        return "--"
    if step <= 0.0:
        return format(value, ".10g")
    text = f"{step:.12f}".rstrip("0").rstrip(".")
    decimals = len(text.partition(".")[2]) if "." in text else 0
    decimals = max(0, min(12, decimals))
    return f"{value:.{decimals}f}"
def _event_time_ms(event: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = event.get(key)
        try:
            number = int(float(value))
        except (TypeError, ValueError, OverflowError):
            continue
        if number > 0:
            return number
    return 0




def _stable_floor_ratio(value: float) -> int:
    """Floor a bucket ratio without losing exact tick boundaries to float ULPs."""
    value = float(value)
    if not math.isfinite(value):
        return 0
    nearest = round(value)
    tolerance = max(1e-12, math.ulp(value) * 8.0)
    if abs(value - nearest) <= tolerance:
        value = float(nearest)
    return int(math.floor(value))
def _nice_tick_multiple(required: float) -> int:
    """Return a stable 1/2/5-style integer multiple at or above *required*."""
    required = max(1.0, float(required))
    exponent = max(0, int(math.floor(math.log10(required))))
    scale = 10 ** exponent
    for base in (1, 2, 5, 10):
        candidate = base * scale
        if candidate >= required:
            return int(candidate)
    return int(10 * scale)


class BookmapHistoryModel:
    """Bounded full-depth temporal history using one exchange-time clock domain.

    Storage orientation is ``[time_column, price_row]``. Time advance therefore
    changes only the write index; a rare price-domain recenter copies only the
    overlapping price-row intersection. Raw ``log1p(quote_notional)`` is stored
    so one low-frequency normalization reference can be applied consistently to
    the complete visible history.
    """

    def __init__(
        self,
        *,
        capture_interval_ms: int = BOOKMAP_CAPTURE_INTERVAL_MS,
        history_ms: int = BOOKMAP_HISTORY_MS,
        price_rows: int = BOOKMAP_PRICE_ROWS,
        stale_timeout_ms: int = BOOKMAP_STALE_TIMEOUT_MS,
    ) -> None:
        self.capture_interval_ms = max(20, int(capture_interval_ms))
        self.history_ms = max(self.capture_interval_ms * 20, int(history_ms))
        self.time_columns = max(20, self.history_ms // self.capture_interval_ms)
        self.price_rows = max(128, int(price_rows))
        self.stale_timeout_ms = max(self.capture_interval_ms * 2, int(stale_timeout_ms))

        shape = (self.time_columns, self.price_rows)
        self.heat = np.zeros(shape, dtype=np.float32)
        self.validity = np.zeros(self.time_columns, dtype=np.bool_)
        self.timestamps_ms = np.zeros(self.time_columns, dtype=np.int64)
        self.best_bid = np.zeros(self.time_columns, dtype=np.float64)
        self.best_ask = np.zeros(self.time_columns, dtype=np.float64)
        self.microprice = np.zeros(self.time_columns, dtype=np.float64)


        self._render_scratch = np.empty(
            (self.price_rows, self.time_columns), dtype=np.float32
        )
        self._validity_scratch = np.empty(self.time_columns, dtype=np.bool_)
        self._timestamp_scratch = np.empty(self.time_columns, dtype=np.int64)

        self.symbol = ""
        self.tick_size = 0.0
        self.price_step = 0.0
        self.price_origin = 0.0
        self._write_index = -1
        self._count = 0
        self._last_bucket_ms = 0
        self._last_actual_depth_ms = 0
        self._time_to_index: dict[int, int] = {}



        self._trade_timestamp_ms = np.zeros(BOOKMAP_TRADE_BIN_CAPACITY, dtype=np.int64)
        self._trade_absolute_bucket = np.zeros(BOOKMAP_TRADE_BIN_CAPACITY, dtype=np.int64)
        self._trade_buy_notional = np.zeros(BOOKMAP_TRADE_BIN_CAPACITY, dtype=np.float64)
        self._trade_sell_notional = np.zeros(BOOKMAP_TRADE_BIN_CAPACITY, dtype=np.float64)
        self._trade_rpi_notional = np.zeros(BOOKMAP_TRADE_BIN_CAPACITY, dtype=np.float64)
        self._trade_key_to_slot: dict[tuple[int, int], int] = {}
        self._trade_next_slot = 0
        self._trade_size = 0
        self._pending_trades: deque[tuple[int, float, float, float, float]] = deque(
            maxlen=BOOKMAP_PENDING_TRADE_CAPACITY
        )
        self._book_valid = False
        self._transport_live = False
        self._validity_reason = "SYNCING"
        self._revision = 0
        self._normalization_low = 0.0
        self._normalization_high = 1.0
        self._last_normalization_mono = 0.0
        self._normalization_initialized = False
        self._normalization_frozen = False
        self._depth_intervals_ms: deque[int] = deque(maxlen=64)
        self._last_depth_column = np.zeros(self.price_rows, dtype=np.float32)
        self._last_best_bid = 0.0
        self._last_best_ask = 0.0
        self._last_microprice = 0.0
        self._recenter_count = 0
        self._continuity_broken = True

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def count(self) -> int:
        return self._count

    @property
    def book_valid(self) -> bool:
        return self._book_valid

    @property
    def transport_live(self) -> bool:
        return self._transport_live

    @property
    def validity_reason(self) -> str:
        return self._validity_reason

    @property
    def recenter_count(self) -> int:
        return self._recenter_count

    @property
    def effective_stale_timeout_ms(self) -> int:
        if len(self._depth_intervals_ms) < 3:
            return self.stale_timeout_ms
        median = float(np.median(np.fromiter(self._depth_intervals_ms, dtype=np.float64)))
        if not math.isfinite(median) or median <= 0.0:
            return self.stale_timeout_ms
        return int(max(BOOKMAP_STALE_TIMEOUT_MIN_MS, min(BOOKMAP_STALE_TIMEOUT_MAX_MS, round(median * 3.0))))

    def set_normalization_frozen(self, frozen: bool) -> None:
        self._normalization_frozen = bool(frozen)

    def reset(self, symbol: str, tick_size: float = 0.0) -> None:
        self.symbol = str(symbol or "").upper()
        self.tick_size = _finite_positive(tick_size)
        self.price_step = 0.0
        self.price_origin = 0.0
        self.heat.fill(0.0)
        self.validity.fill(False)
        self.timestamps_ms.fill(0)
        self.best_bid.fill(0.0)
        self.best_ask.fill(0.0)
        self.microprice.fill(0.0)
        self._write_index = -1
        self._count = 0
        self._last_bucket_ms = 0
        self._last_actual_depth_ms = 0
        self._time_to_index.clear()
        self._trade_timestamp_ms.fill(0)
        self._trade_absolute_bucket.fill(0)
        self._trade_buy_notional.fill(0.0)
        self._trade_sell_notional.fill(0.0)
        self._trade_rpi_notional.fill(0.0)
        self._trade_key_to_slot.clear()
        self._trade_next_slot = 0
        self._trade_size = 0
        self._pending_trades.clear()
        self._book_valid = False
        self._transport_live = False
        self._validity_reason = "SYNCING"
        self._normalization_low = 0.0
        self._normalization_high = 1.0
        self._last_normalization_mono = 0.0
        self._normalization_initialized = False
        self._normalization_frozen = False
        self._depth_intervals_ms.clear()
        self._last_depth_column.fill(0.0)
        self._last_best_bid = 0.0
        self._last_best_ask = 0.0
        self._last_microprice = 0.0
        self._recenter_count = 0
        self._continuity_broken = True
        self._revision += 1

    def set_tick_size(self, tick_size: float) -> None:
        value = _finite_positive(tick_size)
        if value <= 0.0 or math.isclose(
            value, self.tick_size, rel_tol=0.0, abs_tol=1e-15
        ):
            return
        previous = self.tick_size
        if self._count > 0 and previous > 0.0:



            symbol = self.symbol
            live = self._transport_live
            valid = self._book_valid
            reason = self._validity_reason
            self.reset(symbol, value)
            self._transport_live = live
            self._book_valid = valid
            self._validity_reason = reason
            return
        self.tick_size = value


        self.price_step = 0.0

    def set_transport_live(self, live: bool, reason: str = "") -> None:
        live = bool(live)
        if self._transport_live == live and (not reason or reason == self._validity_reason):
            return
        self._transport_live = live
        if not live:
            self._validity_reason = str(reason or "DISCONNECTED")
            self._continuity_broken = True
            self._pending_trades.clear()
        self._revision += 1

    def set_book_validity(self, valid: bool, reason: str = "") -> None:
        valid = bool(valid)
        new_reason = str(reason or ("READY" if valid else "INVALID"))
        if self._book_valid == valid and self._validity_reason == new_reason:
            return
        self._book_valid = valid
        self._validity_reason = new_reason
        if not valid:
            self._continuity_broken = True
            self._pending_trades.clear()
        self._revision += 1

    def _effective_tick(self, bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> float:
        if self.tick_size > 0.0:
            return self.tick_size
        prices: list[float] = []
        prices.extend(float(row[0]) for row in bids[:64] if row[0] > 0.0)
        prices.extend(float(row[0]) for row in asks[:64] if row[0] > 0.0)
        if len(prices) < 2:
            return 1e-8
        unique = sorted(set(prices))
        diffs = [
            unique[index] - unique[index - 1]
            for index in range(1, len(unique))
            if unique[index] > unique[index - 1]
        ]
        return max(1e-12, min(diffs, default=max(abs(unique[0]) * 1e-8, 1e-8)))

    def _choose_auto_step(
        self,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
    ) -> float:
        tick = self._effective_tick(bids, asks)
        bid_sample = bids[: min(256, len(bids))]
        ask_sample = asks[: min(256, len(asks))]
        if not bid_sample or not ask_sample:
            return tick
        low = min(float(row[0]) for row in bid_sample)
        high = max(float(row[0]) for row in ask_sample)
        span = max(tick, high - low)
        usable_rows = max(32.0, self.price_rows * BOOKMAP_TARGET_COVERAGE_FRACTION)
        required_step = max(tick, span / usable_rows)
        multiple = _nice_tick_multiple(required_step / tick)
        return tick * multiple

    def _initialize_price_domain(
        self,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
    ) -> None:
        if self.price_step <= 0.0:
            self.price_step = self._choose_auto_step(bids, asks)
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid = (best_bid + best_ask) * 0.5
        span = self.price_step * self.price_rows
        raw_origin = mid - span * 0.5
        self.price_origin = math.floor(raw_origin / self.price_step) * self.price_step

    def _price_index(self, price: float) -> int:
        if self.price_step <= 0.0:
            return -1



        return self._absolute_price_bucket(float(price)) - self._absolute_price_bucket(
            self.price_origin
        )

    def _recenter_if_needed(self, best_bid: float, best_ask: float) -> None:
        if self.price_step <= 0.0:
            return
        mid = (best_bid + best_ask) * 0.5
        midpoint_index = (mid - self.price_origin) / self.price_step
        guard = self.price_rows * BOOKMAP_GUARD_FRACTION
        if guard <= midpoint_index <= self.price_rows - guard:
            return

        raw_origin = mid - self.price_rows * self.price_step * 0.5
        new_origin = math.floor(raw_origin / self.price_step) * self.price_step
        shift = int(round((self.price_origin - new_origin) / self.price_step))
        if shift == 0:
            return

        started = time.perf_counter()
        new_heat = np.zeros_like(self.heat)

        if abs(shift) < self.price_rows:
            if shift > 0:
                old_slice = slice(0, self.price_rows - shift)
                new_slice = slice(shift, self.price_rows)
            else:
                old_slice = slice(-shift, self.price_rows)
                new_slice = slice(0, self.price_rows + shift)
            new_heat[:, new_slice] = self.heat[:, old_slice]



        self.heat = new_heat
        self.price_origin = new_origin
        self._last_depth_column = np.roll(self._last_depth_column, shift)
        if shift > 0:
            self._last_depth_column[:shift] = 0.0
        else:
            self._last_depth_column[self.price_rows + shift :] = 0.0
        self._recenter_count += 1
        self._normalization_initialized = False
        self._last_normalization_mono = 0.0
        self._revision += 1

    def _depth_column(
        self,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
    ) -> np.ndarray:
        """Aggregate the full cleaned book into one quote-notional heat column.

        The parser already hands us numeric cleaned levels.  Vectorizing the
        price-bucket transform keeps 1,000×2 level books comfortably below the
        GUI-thread budget while preserving exact stable bucket semantics.
        """
        level_count = len(bids) + len(asks)
        if level_count <= 0 or self.price_step <= 0.0:
            return np.zeros(self.price_rows, dtype=np.float32)

        prices = np.fromiter(
            (float(row[0]) for rows in (bids, asks) for row in rows),
            dtype=np.float64,
            count=level_count,
        )
        quantities = np.fromiter(
            (float(row[1]) for rows in (bids, asks) for row in rows),
            dtype=np.float64,
            count=level_count,
        )
        valid = (
            np.isfinite(prices)
            & np.isfinite(quantities)
            & (prices > 0.0)
            & (quantities > 0.0)
        )
        if not np.any(valid):
            return np.zeros(self.price_rows, dtype=np.float32)
        prices = prices[valid]
        quantities = quantities[valid]

        ratios = prices / self.price_step
        nearest = np.rint(ratios)
        tolerance = np.maximum(1e-12, np.abs(np.spacing(ratios)) * 8.0)
        ratios = np.where(np.abs(ratios - nearest) <= tolerance, nearest, ratios)
        absolute_buckets = np.floor(ratios).astype(np.int64)
        origin_bucket = self._absolute_price_bucket(self.price_origin)
        indices = absolute_buckets - origin_bucket
        inside = (indices >= 0) & (indices < self.price_rows)
        if not np.any(inside):
            return np.zeros(self.price_rows, dtype=np.float32)

        column = np.bincount(
            indices[inside],
            weights=prices[inside] * quantities[inside],
            minlength=self.price_rows,
        ).astype(np.float64, copy=False)
        if column.size > self.price_rows:
            column = column[: self.price_rows]
        np.log1p(column, out=column)
        return column.astype(np.float32, copy=False)

    def _absolute_price_bucket(self, price: float) -> int:
        if self.price_step <= 0.0:
            return 0
        return _stable_floor_ratio(float(price) / self.price_step)

    def _store_trade_bin(
        self,
        bucket_ms: int,
        price: float,
        buy_notional: float,
        sell_notional: float,
        rpi_notional: float,
    ) -> bool:
        """Accumulate one execution into the fixed sparse time×price ring.

        The key index gives O(1) aggregation for repeated prints in the same
        100 ms × price bucket. New keys overwrite one preallocated slot; no
        Python trade objects or dense time×price trade rasters are created.
        """
        if self.price_step <= 0.0:
            return False
        bucket_ms = int(bucket_ms)
        absolute_bucket = int(self._absolute_price_bucket(price))
        key = (bucket_ms, absolute_bucket)

        slot = self._trade_key_to_slot.get(key)
        if slot is not None:
            if (
                int(self._trade_timestamp_ms[slot]) != bucket_ms
                or int(self._trade_absolute_bucket[slot]) != absolute_bucket
            ):
                self._trade_key_to_slot.pop(key, None)
                slot = None

        if slot is None:
            slot = self._trade_next_slot
            old_time = int(self._trade_timestamp_ms[slot])
            if old_time > 0:
                old_key = (old_time, int(self._trade_absolute_bucket[slot]))
                self._trade_key_to_slot.pop(old_key, None)
            else:
                self._trade_size = min(
                    BOOKMAP_TRADE_BIN_CAPACITY, self._trade_size + 1
                )

            self._trade_timestamp_ms[slot] = bucket_ms
            self._trade_absolute_bucket[slot] = absolute_bucket
            self._trade_buy_notional[slot] = 0.0
            self._trade_sell_notional[slot] = 0.0
            self._trade_rpi_notional[slot] = 0.0
            self._trade_key_to_slot[key] = slot
            self._trade_next_slot = (slot + 1) % BOOKMAP_TRADE_BIN_CAPACITY

        self._trade_buy_notional[slot] += max(0.0, float(buy_notional))
        self._trade_sell_notional[slot] += max(0.0, float(sell_notional))
        self._trade_rpi_notional[slot] += max(0.0, float(rpi_notional))
        return True

    def _flush_pending_trades(self) -> None:
        if self.price_step <= 0.0 or not self._pending_trades:
            return
        while self._pending_trades:
            bucket_ms, price, buy_notional, sell_notional, rpi_notional = (
                self._pending_trades.popleft()
            )
            if (
                self._last_bucket_ms
                and bucket_ms < self._last_bucket_ms - self.history_ms
            ):
                continue
            self._store_trade_bin(
                bucket_ms, price, buy_notional, sell_notional, rpi_notional
            )

    def _trade_slot_for(self, bucket_ms: int, absolute_bucket: int) -> int | None:
        key = (int(bucket_ms), int(absolute_bucket))
        slot = self._trade_key_to_slot.get(key)
        if slot is None:
            return None
        if (
            int(self._trade_timestamp_ms[slot]) != key[0]
            or int(self._trade_absolute_bucket[slot]) != key[1]
        ):
            self._trade_key_to_slot.pop(key, None)
            return None
        return int(slot)

    def _advance_column(
        self,
        bucket_ms: int,
        column: np.ndarray | None,
        *,
        valid: bool,
        best_bid: float = 0.0,
        best_ask: float = 0.0,
        microprice: float = 0.0,
    ) -> int:
        next_index = 0 if self._write_index < 0 else (self._write_index + 1) % self.time_columns
        if self._count == self.time_columns:
            old_time = int(self.timestamps_ms[next_index])
            if old_time:
                self._time_to_index.pop(old_time, None)
        else:
            self._count += 1

        self._write_index = next_index
        self.timestamps_ms[next_index] = int(bucket_ms)
        self._time_to_index[int(bucket_ms)] = next_index
        self.validity[next_index] = bool(valid)
        self.heat[next_index].fill(0.0)
        self.best_bid[next_index] = float(best_bid if valid else 0.0)
        self.best_ask[next_index] = float(best_ask if valid else 0.0)
        self.microprice[next_index] = float(microprice if valid else 0.0)
        if valid and column is not None:
            np.copyto(self.heat[next_index], column)
        self._last_bucket_ms = int(bucket_ms)
        self._revision += 1
        return next_index

    def ingest_depth(
        self,
        timestamp_ms: int,
        bids: Iterable[tuple[float, float]],
        asks: Iterable[tuple[float, float]],
    ) -> bool:
        started = time.perf_counter()
        timestamp_ms = int(timestamp_ms)
        if timestamp_ms <= 0:
            return False
        bids_list = list(bids)
        asks_list = list(asks)
        if not bids_list or not asks_list:
            return False
        best_bid = _finite_positive(bids_list[0][0])
        best_ask = _finite_positive(asks_list[0][0])
        if best_bid <= 0.0 or best_ask <= best_bid:
            return False

        bucket_ms = timestamp_ms - (timestamp_ms % self.capture_interval_ms)
        if self._last_bucket_ms and bucket_ms < self._last_bucket_ms:
            return False

        if self.price_step <= 0.0:
            self._initialize_price_domain(bids_list, asks_list)
            self._flush_pending_trades()
        self._recenter_if_needed(best_bid, best_ask)
        column = self._depth_column(bids_list, asks_list)
        bid_quantity = _finite_positive(bids_list[0][1])
        ask_quantity = _finite_positive(asks_list[0][1])
        top_quantity = bid_quantity + ask_quantity
        microprice = (
            (best_ask * bid_quantity + best_bid * ask_quantity) / top_quantity
            if top_quantity > 0.0
            else (best_bid + best_ask) * 0.5
        )
        currently_valid = bool(self._transport_live and self._book_valid)

        if self._last_bucket_ms and bucket_ms > self._last_bucket_ms:
            next_bucket = self._last_bucket_ms + self.capture_interval_ms
            while next_bucket < bucket_ms:
                age_from_real = next_bucket - self._last_actual_depth_ms
                carry = (
                    currently_valid
                    and not self._continuity_broken
                    and self._last_actual_depth_ms > 0
                    and age_from_real <= self.effective_stale_timeout_ms
                )
                if carry:
                    self._advance_column(
                        next_bucket,
                        self._last_depth_column,
                        valid=True,
                        best_bid=self._last_best_bid,
                        best_ask=self._last_best_ask,
                        microprice=self._last_microprice,
                    )
                else:
                    self._advance_column(next_bucket, None, valid=False)
                next_bucket += self.capture_interval_ms

        if self._last_bucket_ms == bucket_ms and self._write_index >= 0:
            index = self._write_index
            self.validity[index] = currently_valid
            self.heat[index].fill(0.0)
            if currently_valid:
                np.copyto(self.heat[index], column)
                self.best_bid[index] = best_bid
                self.best_ask[index] = best_ask
                self.microprice[index] = microprice
            else:
                self.best_bid[index] = 0.0
                self.best_ask[index] = 0.0
                self.microprice[index] = 0.0
            self._revision += 1
        else:
            self._advance_column(
                bucket_ms,
                column if currently_valid else None,
                valid=currently_valid,
                best_bid=best_bid,
                best_ask=best_ask,
                microprice=microprice,
            )

        if self._last_actual_depth_ms > 0:
            interval_ms = timestamp_ms - self._last_actual_depth_ms
            if 0 < interval_ms <= 2_000:
                self._depth_intervals_ms.append(int(interval_ms))
        self._last_actual_depth_ms = timestamp_ms
        self._last_depth_column = column
        self._last_best_bid = best_bid
        self._last_best_ask = best_ask
        self._last_microprice = microprice
        if currently_valid:
            self._continuity_broken = False
        return True

    def advance_to(self, timestamp_ms: int) -> int:
        """Advance the fixed 100 ms exchange-time axis without a depth event.

        Valid liquidity is carried only while transport/book validity is intact,
        continuity has not been broken, and the last real depth publication is
        inside the staleness budget. Beyond that boundary explicit invalid
        columns are written, so a quiet or stalled feed cannot fabricate a
        persistent wall.
        """
        timestamp_ms = int(timestamp_ms)
        if timestamp_ms <= 0 or self._last_bucket_ms <= 0:
            return 0
        target_bucket = timestamp_ms - (timestamp_ms % self.capture_interval_ms)
        if target_bucket <= self._last_bucket_ms:
            return 0

        currently_valid = bool(self._transport_live and self._book_valid)
        written = 0
        next_bucket = self._last_bucket_ms + self.capture_interval_ms
        while next_bucket <= target_bucket:
            age_from_real = next_bucket - self._last_actual_depth_ms
            carry = (
                currently_valid
                and not self._continuity_broken
                and self._last_actual_depth_ms > 0
                and age_from_real <= self.effective_stale_timeout_ms
            )
            if carry:
                self._advance_column(
                    next_bucket,
                    self._last_depth_column,
                    valid=True,
                    best_bid=self._last_best_bid,
                    best_ask=self._last_best_ask,
                    microprice=self._last_microprice,
                )
            else:
                self._advance_column(next_bucket, None, valid=False)
            next_bucket += self.capture_interval_ms
            written += 1
        if written:
            pass
        return written

    def ingest_trade_event(self, event: dict[str, Any]) -> bool:
        if str(event.get("s") or "").upper() != self.symbol:
            return False
        timestamp_ms = _event_time_ms(event, "T", "E")
        price = _finite_positive(event.get("p"))
        quantity = _finite_positive(event.get("q"))
        maker_flag = event.get("m")
        if timestamp_ms <= 0 or price <= 0.0 or quantity <= 0.0 or not isinstance(maker_flag, bool):
            return False
        if not (self._transport_live and self._book_valid):
            return False

        bucket_ms = timestamp_ms - (timestamp_ms % self.capture_interval_ms)
        notional = price * quantity
        buy_notional = 0.0 if maker_flag else notional
        sell_notional = notional if maker_flag else 0.0
        rpi_notional = max(0.0, float(event.get("_rpi_notional") or 0.0))

        if self._last_bucket_ms and bucket_ms < self._last_bucket_ms - self.history_ms:
            return False
        if self.price_step <= 0.0:
            if len(self._pending_trades) == self._pending_trades.maxlen:
                pass
            self._pending_trades.append(
                (bucket_ms, price, buy_notional, sell_notional, rpi_notional)
            )
            return True





        return self._store_trade_bin(
            bucket_ms, price, buy_notional, sell_notional, rpi_notional
        )

    def ingest_trade_batch(self, events: Iterable[dict[str, Any]]) -> int:
        count = 0
        for event in events:
            if self.ingest_trade_event(event):
                count += 1
        if count:
            pass
        return count

    def _chronological_copy(self, source: np.ndarray, target: np.ndarray) -> None:
        count = self._count
        if count <= 0:
            return
        start = (self._write_index - count + 1) % self.time_columns
        first = min(count, self.time_columns - start)
        if source.ndim == 2:
            target[:, :first] = source[start : start + first].T
            remaining = count - first
            if remaining:
                target[:, first:count] = source[:remaining].T
        else:
            target[:first] = source[start : start + first]
            remaining = count - first
            if remaining:
                target[first:count] = source[:remaining]

    def _chronological_vector(self, source: np.ndarray) -> np.ndarray:
        output = np.empty(self._count, dtype=source.dtype)
        if self._count:
            self._chronological_copy(source, output)
        return output

    def normalization_high(self, *, force: bool = False) -> float:
        now = time.monotonic()
        if self._normalization_frozen and self._normalization_initialized and not force:
            return self._normalization_high
        if not force and now - self._last_normalization_mono < 2.0:
            return self._normalization_high
        count = self._count
        if count <= 0:
            return self._normalization_high




        recent_columns = min(count, max(1, BOOKMAP_DEFAULT_VISIBLE_MS // self.capture_interval_ms))
        start_offset = count - recent_columns
        chronological = self._render_scratch[:, :recent_columns]
        ring_start = (self._write_index - count + 1 + start_offset) % self.time_columns
        first = min(recent_columns, self.time_columns - ring_start)
        chronological[:, :first] = self.heat[ring_start : ring_start + first].T
        remaining = recent_columns - first
        if remaining:
            chronological[:, first:recent_columns] = self.heat[:remaining].T

        valid = self._validity_scratch[:recent_columns]
        valid[:first] = self.validity[ring_start : ring_start + first]
        if remaining:
            valid[first:recent_columns] = self.validity[:remaining]




        sample_cap = 32_768
        total_cells = max(1, chronological.shape[0] * chronological.shape[1])
        stride = max(1, int(math.ceil(math.sqrt(total_cells / sample_cap))))
        row_offset = int((self._revision * 17) % stride)
        col_offset = int((self._revision * 31) % stride)
        sampled = chronological[row_offset::stride, col_offset::stride]
        sampled_valid = valid[col_offset::stride]
        if sampled.size and np.any(sampled_valid):
            values = sampled[:, sampled_valid].reshape(-1)
        else:
            values = np.empty(0, dtype=np.float32)
        nonzero = values[np.isfinite(values) & (values > 0.0)]
        if nonzero.size < 64 and np.any(valid):




            full_values = chronological[:, valid]
            full_nonzero = full_values[
                np.isfinite(full_values) & (full_values > 0.0)
            ]
            if full_nonzero.size:
                nonzero = full_nonzero
        if nonzero.size:
            candidate_low, candidate_high = np.percentile(
                nonzero,
                (BOOKMAP_NORMALIZATION_FLOOR_PERCENTILE, BOOKMAP_NORMALIZATION_PERCENTILE),
            )
            candidate_low = float(candidate_low)
            candidate_high = float(candidate_high)
            if math.isfinite(candidate_low) and math.isfinite(candidate_high) and candidate_high > candidate_low:
                commit = force or not self._normalization_initialized
                if self._normalization_initialized and not commit:
                    hi_move = abs(candidate_high - self._normalization_high) / max(abs(self._normalization_high), 1e-6)
                    span = max(self._normalization_high - self._normalization_low, 1e-6)
                    lo_move = abs(candidate_low - self._normalization_low) / span
                    commit = hi_move >= BOOKMAP_NORMALIZATION_HYSTERESIS or lo_move >= BOOKMAP_NORMALIZATION_HYSTERESIS
                if commit:
                    self._normalization_low = max(0.0, candidate_low)
                    self._normalization_high = max(self._normalization_low + 1e-6, candidate_high)
                    self._normalization_initialized = True
        self._last_normalization_mono = now
        return self._normalization_high

    def _window_count(self, window_ms: int | None) -> int:
        if window_ms is None or window_ms <= 0:
            return self._count
        return min(self._count, max(1, int(math.ceil(window_ms / self.capture_interval_ms))))

    def _window_ring_start(self, columns: int) -> int:
        return (self._write_index - columns + 1) % self.time_columns

    def render_image(self, *, window_ms: int | None = None) -> np.ndarray:
        columns = self._window_count(window_ms)
        target = self._render_scratch[:, :columns]
        if columns <= 0:
            return target
        start = self._window_ring_start(columns)
        first = min(columns, self.time_columns - start)
        target[:, :first] = self.heat[start : start + first].T
        remaining = columns - first
        if remaining:
            target[:, first:columns] = self.heat[:remaining].T
        return target

    def _latest_values(self) -> tuple[float, float, float]:
        if self._write_index < 0:
            return 0.0, 0.0, 0.0
        return (
            float(self.best_bid[self._write_index]),
            float(self.best_ask[self._write_index]),
            float(self.microprice[self._write_index]),
        )

    def render_frame(self, *, window_ms: int | None = BOOKMAP_DEFAULT_VISIBLE_MS) -> BookmapRenderFrame | None:
        columns = self._window_count(window_ms)
        if columns <= 0 or self.price_step <= 0.0:
            return None
        normalization_high = self.normalization_high()
        image = self.render_image(window_ms=window_ms)
        start = self._window_ring_start(columns)
        first = min(columns, self.time_columns - start)
        timestamps = self._timestamp_scratch[:columns]
        validity = self._validity_scratch[:columns]
        timestamps[:first] = self.timestamps_ms[start : start + first]
        validity[:first] = self.validity[start : start + first]
        remaining = columns - first
        if remaining:
            timestamps[first:columns] = self.timestamps_ms[:remaining]
            validity[first:columns] = self.validity[:remaining]
        best_bid, best_ask, microprice = self._latest_values()
        return BookmapRenderFrame(
            revision=self._revision,
            symbol=self.symbol,
            image=image,
            validity=validity,
            oldest_time_s=float(timestamps[0]) / 1000.0,
            newest_time_s=float(timestamps[-1]) / 1000.0,
            price_origin=self.price_origin,
            price_step=self.price_step,
            best_bid=best_bid,
            best_ask=best_ask,
            microprice=microprice,
            normalization_low=self._normalization_low,
            normalization_high=normalization_high,
            live=self._transport_live,
            book_valid=self._book_valid,
            validity_reason=self._validity_reason,
        )

    def price_trace_points(self, *, start_time_s: float | None = None) -> tuple[np.ndarray, np.ndarray]:
        count = self._count
        if count <= 0:
            empty = np.empty(0, dtype=np.float64)
            return empty, empty
        timestamps = self._chronological_vector(self.timestamps_ms).astype(np.float64, copy=False)
        microprice = self._chronological_vector(self.microprice).astype(np.float64, copy=False)
        validity = self._chronological_vector(self.validity)
        y = microprice.copy()
        y[(~validity) | (~np.isfinite(y)) | (y <= 0.0)] = np.nan
        x = timestamps / 1000.0
        if start_time_s is not None:
            keep = x >= float(start_time_s)
            x = x[keep]
            y = y[keep]
        return x, y

    def trade_points(
        self,
        *,
        start_time_s: float | None = None,
        end_time_s: float | None = None,
    ) -> BookmapTradePoints:
        """Return significant visible execution bins using vectorized filters."""
        empty = np.empty(0, dtype=np.float64)
        if (
            self._count <= 0
            or self.price_step <= 0.0
            or self._trade_size <= 0
            or self._write_index < 0
        ):
            return BookmapTradePoints(
                empty, empty, empty, empty, empty, empty, empty
            )

        started = time.perf_counter()
        start_ms = (
            int(float(start_time_s) * 1000.0)
            if start_time_s is not None
            else int(self._last_bucket_ms - self.history_ms)
        )
        end_ms = (
            int(float(end_time_s) * 1000.0)
            if end_time_s is not None
            else int(self._last_bucket_ms)
        )
        if end_ms < start_ms:
            start_ms, end_ms = end_ms, start_ms

        timestamps = self._trade_timestamp_ms
        active = (timestamps >= start_ms) & (timestamps <= end_ms) & (timestamps > 0)
        slots = np.flatnonzero(active)
        if slots.size == 0:
            return BookmapTradePoints(
                empty, empty, empty, empty, empty, empty, empty
            )

        timestamp_values = timestamps[slots]
        delta_ms = int(self._last_bucket_ms) - timestamp_values
        aligned = (delta_ms >= 0) & (delta_ms % self.capture_interval_ms == 0)
        delta_columns = np.floor_divide(
            np.maximum(delta_ms, 0), self.capture_interval_ms
        )
        aligned &= delta_columns < self._count
        if not np.any(aligned):
            return BookmapTradePoints(
                empty, empty, empty, empty, empty, empty, empty
            )

        slots = slots[aligned]
        timestamp_values = timestamp_values[aligned]
        delta_columns = delta_columns[aligned]
        time_indices = (self._write_index - delta_columns) % self.time_columns
        valid_depth = (
            self.timestamps_ms[time_indices] == timestamp_values
        ) & self.validity[time_indices]
        if not np.any(valid_depth):
            return BookmapTradePoints(
                empty, empty, empty, empty, empty, empty, empty
            )

        slots = slots[valid_depth]
        timestamp_values = timestamp_values[valid_depth]
        absolute_buckets = self._trade_absolute_bucket[slots]
        y = (absolute_buckets.astype(np.float64) + 0.5) * self.price_step
        origin_bucket = self._absolute_price_bucket(self.price_origin)
        rows = absolute_buckets - origin_bucket
        visible_price = (rows >= 0) & (rows < self.price_rows)
        if not np.any(visible_price):
            return BookmapTradePoints(
                empty, empty, empty, empty, empty, empty, empty
            )

        slots = slots[visible_price]
        timestamp_values = timestamp_values[visible_price]
        y = y[visible_price]
        buy_values = self._trade_buy_notional[slots]
        sell_values = self._trade_sell_notional[slots]
        rpi_values = self._trade_rpi_notional[slots]
        totals = buy_values + sell_values
        positive = np.isfinite(totals) & (totals > 0.0)
        if not np.any(positive):
            return BookmapTradePoints(
                empty, empty, empty, empty, empty, empty, empty
            )

        timestamp_values = timestamp_values[positive]
        y = y[positive]
        buy_values = buy_values[positive]
        sell_values = sell_values[positive]
        rpi_values = rpi_values[positive]
        totals = totals[positive]

        if totals.size > 1:
            threshold = float(np.percentile(totals, 70.0))
            keep = totals >= threshold
            timestamp_values = timestamp_values[keep]
            y = y[keep]
            buy_values = buy_values[keep]
            sell_values = sell_values[keep]
            rpi_values = rpi_values[keep]
            totals = totals[keep]

        if totals.size > BOOKMAP_MAX_TRADE_MARKERS:
            keep = np.argpartition(
                totals, totals.size - BOOKMAP_MAX_TRADE_MARKERS
            )[-BOOKMAP_MAX_TRADE_MARKERS:]
            timestamp_values = timestamp_values[keep]
            y = y[keep]
            buy_values = buy_values[keep]
            sell_values = sell_values[keep]
            rpi_values = rpi_values[keep]
            totals = totals[keep]

        order = np.argsort(timestamp_values, kind="stable")
        timestamp_values = timestamp_values[order]
        y = y[order]
        buy_values = buy_values[order]
        sell_values = sell_values[order]
        rpi_values = rpi_values[order]
        totals = totals[order]

        x = timestamp_values.astype(np.float64, copy=False) / 1000.0
        trade_lo = float(np.percentile(totals, 50.0))
        trade_hi = float(np.percentile(totals, 99.0))
        log_lo = math.log1p(max(trade_lo, 0.0))
        log_hi = math.log1p(max(trade_hi, trade_lo + 1e-9))
        if log_hi - log_lo <= 1e-9:
            v = np.ones_like(totals)
        else:
            v = np.clip(
                (np.log1p(totals) - log_lo) / (log_hi - log_lo),
                0.0,
                1.0,
            )
        size = np.sqrt(4.0**2 + v * (18.0**2 - 4.0**2))
        imbalance = (buy_values - sell_values) / np.maximum(totals, 1e-9)
        rpi_fraction = rpi_values / np.maximum(totals, 1e-9)
        return BookmapTradePoints(
            x, y, size, totals, imbalance, rpi_values, rpi_fraction
        )

    def inspect(self, time_s: float, price: float) -> dict[str, float | int | bool]:
        if self._count <= 0 or self.price_step <= 0.0:
            return {}
        bucket_ms = int(round((float(time_s) * 1000.0) / self.capture_interval_ms)) * self.capture_interval_ms
        index = self._time_to_index.get(bucket_ms)
        if index is None:

            timestamps = self._chronological_vector(self.timestamps_ms)
            if not timestamps.size:
                return {}
            pos = int(np.argmin(np.abs(timestamps - bucket_ms)))
            bucket_ms = int(timestamps[pos])
            index = self._time_to_index.get(bucket_ms)
            if index is None:
                return {}
        row = self._price_index(float(price))
        if not 0 <= row < self.price_rows:
            return {}
        valid = bool(self.validity[index])
        log_value = float(self.heat[index, row]) if valid else 0.0
        resting = math.expm1(log_value) if log_value > 0.0 else 0.0
        cell_price = self.price_origin + (row + 0.5) * self.price_step
        trade_slot = (
            self._trade_slot_for(
                bucket_ms, self._absolute_price_bucket(cell_price)
            )
            if valid
            else None
        )
        buy = (
            float(self._trade_buy_notional[trade_slot])
            if trade_slot is not None
            else 0.0
        )
        sell = (
            float(self._trade_sell_notional[trade_slot])
            if trade_slot is not None
            else 0.0
        )
        rpi = (
            float(self._trade_rpi_notional[trade_slot])
            if trade_slot is not None
            else 0.0
        )

        persistence = 0.0
        if resting > 0.0:
            cursor = index
            steps = 0
            while steps < self._count:
                if not self.validity[cursor] or self.heat[cursor, row] <= 0.0:
                    break
                persistence += self.capture_interval_ms / 1000.0
                steps += 1
                cursor = (cursor - 1) % self.time_columns
                if cursor == self._write_index:
                    break
        timestamps = self._chronological_vector(self.timestamps_ms)
        heat_row = np.empty(self._count, dtype=np.float32)
        validity = self._chronological_vector(self.validity)
        start = (self._write_index - self._count + 1) % self.time_columns
        first = min(self._count, self.time_columns - start)
        heat_row[:first] = self.heat[start : start + first, row]
        if self._count - first:
            heat_row[first:] = self.heat[: self._count - first, row]
        pos = int(np.argmin(np.abs(timestamps - bucket_ms)))
        present = validity & np.isfinite(heat_row) & (heat_row > 0.0)
        peak_log = float(np.max(heat_row[present])) if np.any(present) else 0.0
        peak = math.expm1(peak_log) if peak_log > 0.0 else 0.0
        presence = float(np.count_nonzero(present)) * self.capture_interval_ms / 1000.0
        def prior_notional(seconds: float) -> float:
            offset = max(1, int(round(seconds * 1000.0 / self.capture_interval_ms)))
            prior = max(0, pos - offset)
            value = float(heat_row[prior]) if validity[prior] else 0.0
            return math.expm1(value) if value > 0.0 else 0.0
        micro = float(self._chronological_vector(self.microprice)[pos]) if self._count else 0.0
        distance_ticks = (cell_price - micro) / max(self.tick_size or self.price_step, 1e-12) if micro > 0.0 else 0.0
        distance_bps = ((cell_price - micro) / micro * 10_000.0) if micro > 0.0 else 0.0
        return {
            "timestamp_ms": bucket_ms,
            "price": cell_price,
            "valid": valid,
            "resting_notional": resting,
            "buy_notional": buy,
            "sell_notional": sell,
            "rpi_notional": rpi,
            "persistence_seconds": persistence,
            "presence_seconds": presence,
            "peak_notional": peak,
            "change_1s": resting - prior_notional(1.0),
            "change_5s": resting - prior_notional(5.0),
            "distance_ticks": distance_ticks,
            "distance_bps": distance_bps,
        }


class BookmapController:
    """Small ownership facade between MainWindow's market lifecycle and the model."""

    def __init__(self, symbol: str = "", tick_size: float = 0.0) -> None:
        self.model = BookmapHistoryModel()
        self._exchange_anchor_ms = 0
        self._exchange_anchor_mono = 0.0
        self.model.reset(symbol, tick_size)

    def set_symbol(self, symbol: str, tick_size: float = 0.0) -> None:
        self._exchange_anchor_ms = 0
        self._exchange_anchor_mono = 0.0
        self.model.reset(symbol, tick_size)

    def set_tick_size(self, tick_size: float) -> None:
        self.model.set_tick_size(tick_size)

    def set_transport_live(self, live: bool, reason: str = "") -> None:
        self.model.set_transport_live(live, reason)
        self.advance_clock()

    def set_book_validity(self, valid: bool, reason: str = "") -> None:
        self.model.set_book_validity(valid, reason)
        self.advance_clock()

    def advance_clock(self, monotonic_now: float | None = None) -> int:
        """Advance using an exchange-time estimate anchored by the last depth event."""
        if self._exchange_anchor_ms <= 0 or self._exchange_anchor_mono <= 0.0:
            return 0
        now = time.monotonic() if monotonic_now is None else float(monotonic_now)
        elapsed_ms = max(0.0, (now - self._exchange_anchor_mono) * 1000.0)
        estimated_exchange_ms = (
            self._exchange_anchor_ms
            + int(elapsed_ms)
            - BOOKMAP_CLOCK_SAFETY_LAG_MS
        )
        return self.model.advance_to(estimated_exchange_ms)

    def ingest_depth(
        self,
        event: dict[str, Any],
        bids: Iterable[tuple[float, float]],
        asks: Iterable[tuple[float, float]],
    ) -> bool:
        timestamp_ms = _event_time_ms(event, "E", "T")
        accepted = self.model.ingest_depth(timestamp_ms, bids, asks)
        if accepted and timestamp_ms > 0:
            server_now_ms = _event_time_ms(event, "_server_received_ms")





            self._exchange_anchor_ms = max(timestamp_ms, server_now_ms)
            self._exchange_anchor_mono = time.monotonic()
        return accepted

    def ingest_trade_batch(self, payloads: Iterable[dict[str, Any]]) -> int:
        return self.model.ingest_trade_batch(payloads)




try:  # pragma: no cover - exercised in the real application runtime.
    import pyqtgraph as pg
    from PySide6 import QtCore, QtGui, QtWidgets
    from PySide6.QtCore import Qt, QTimer, Signal

    from .models import human_number
    from .utilities import ElidedLabel, TextRole, typography_font
except ModuleNotFoundError:  # pragma: no cover - used by headless validation.
    pg = None  # type: ignore[assignment]
    QtCore = QtGui = QtWidgets = None  # type: ignore[assignment]
    Qt = QTimer = Signal = None  # type: ignore[assignment]


BOOKMAP_GUI_AVAILABLE = pg is not None and QtWidgets is not None


if BOOKMAP_GUI_AVAILABLE:  # pragma: no branch

    class BookmapTimeAxis(pg.AxisItem):
        def tickStrings(self, values: list[float], scale: float, spacing: float) -> list[str]:
            labels: list[str] = []
            include_seconds = spacing < 60.0
            for value in values:
                try:
                    stamp = datetime.fromtimestamp(float(value) * scale, tz=timezone.utc)
                except (OverflowError, OSError, ValueError):
                    labels.append("")
                    continue
                labels.append(stamp.strftime("%H:%M:%S" if include_seconds else "%H:%M"))
            return labels


    class BookmapPriceAxis(pg.AxisItem):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._step = 0.0

        def set_step(self, step: float) -> None:
            self._step = max(0.0, float(step))
            self.picture = None
            self.update()

        def tickStrings(self, values: list[float], scale: float, spacing: float) -> list[str]:
            spacing_value = abs(float(spacing) * float(scale))
            display_step = self._step
            if spacing_value > 0.0 and (display_step <= 0.0 or spacing_value < display_step):
                display_step = spacing_value
            return [_format_bookmap_price(float(value) * scale, display_step) for value in values]


    class BookmapViewBox(pg.ViewBox):
        manual_navigation = Signal()

        def __init__(self) -> None:
            super().__init__(enableMenu=False)
            self.setMouseMode(self.PanMode)

        def mouseDragEvent(self, event: Any, axis: int | None = None) -> None:
            if event.isStart():
                self.manual_navigation.emit()
            super().mouseDragEvent(event, axis=axis)

        def wheelEvent(self, event: Any, axis: int | None = None) -> None:
            self.manual_navigation.emit()
            super().wheelEvent(event, axis=axis)


    class BookmapWidget(QtWidgets.QWidget):
        """Edge-first temporal liquidity surface backed by validated L2 depth.

        Visual hierarchy is intentionally strict:
        heat = resting liquidity, bubbles = executions, line = microprice,
        sparse right-edge cues = current BBO/order-flow/account context.
        """

        live_requested = Signal()

        def __init__(
            self,
            controller: BookmapController,
            theme: dict[str, Any],
            parent: QtWidgets.QWidget | None = None,
        ) -> None:
            super().__init__(parent)
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.controller = controller
            self.model = controller.model
            self.theme = dict(theme)
            self._last_revision = -1
            self._auto_follow = True
            self._viewport_dirty = True
            self._execution_context: object | None = None
            self._order_flow_snapshot: object | None = None
            self._account_items: list[tuple[Any, Any, float, str]] = []
            self._last_trade_points: BookmapTradePoints | None = None
            self._last_frame: BookmapRenderFrame | None = None
            self._pinned = False
            self._pinned_x = 0.0
            self._pinned_y = 0.0

            root = QtWidgets.QVBoxLayout(self)
            root.setContentsMargins(0, 0, 0, 0)
            root.setSpacing(0)

            toolbar = QtWidgets.QWidget(self)
            toolbar.setFixedHeight(24)
            toolbar_layout = QtWidgets.QHBoxLayout(toolbar)
            toolbar_layout.setContentsMargins(5, 1, 4, 1)
            toolbar_layout.setSpacing(5)
            self.status_label = ElidedLabel("● SYNCING · 60s", toolbar)
            self.status_label.setFont(typography_font(TextRole.ORDERBOOK_LABEL))
            self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
            toolbar_layout.addWidget(self.status_label, 1)

            self.trades_button = QtWidgets.QToolButton(toolbar)
            self.trades_button.setText("TRADES")
            self.trades_button.setCheckable(True)
            self.trades_button.setChecked(True)
            self.trades_button.setFixedHeight(20)
            self.trades_button.setFont(typography_font(TextRole.ORDERBOOK_LABEL))
            self.trades_button.toggled.connect(self._trades_toggled)
            toolbar_layout.addWidget(self.trades_button, 0)

            self.live_button = QtWidgets.QToolButton(toolbar)
            self.live_button.setText("LIVE →")
            self.live_button.setFixedHeight(20)
            self.live_button.setFont(typography_font(TextRole.ORDERBOOK_LABEL))
            self.live_button.clicked.connect(self.go_live)
            self.live_button.hide()
            toolbar_layout.addWidget(self.live_button, 0)
            root.addWidget(toolbar, 0)
            self.toolbar = toolbar

            inspector = QtWidgets.QWidget(self)
            inspector.setFixedHeight(34)
            inspector_layout = QtWidgets.QVBoxLayout(inspector)
            inspector_layout.setContentsMargins(5, 1, 5, 1)
            inspector_layout.setSpacing(0)
            self.inspect_primary = ElidedLabel("Hover to inspect displayed liquidity", inspector)
            self.inspect_secondary = ElidedLabel("", inspector)
            for label in (self.inspect_primary, self.inspect_secondary):
                label.setFont(typography_font(TextRole.ORDERBOOK_LABEL))
                label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
                inspector_layout.addWidget(label, 1)
            root.addWidget(inspector, 0)
            self.inspector = inspector

            self.price_axis = BookmapPriceAxis(orientation="right")
            self.time_axis = BookmapTimeAxis(orientation="bottom")
            axis_font = typography_font(TextRole.CHART_AXIS)
            self.price_axis.setStyle(tickFont=axis_font)
            self.time_axis.setStyle(tickFont=axis_font)
            self.view_box = BookmapViewBox()
            self.plot = pg.PlotWidget(
                parent=self,
                viewBox=self.view_box,
                axisItems={"right": self.price_axis, "bottom": self.time_axis},
            )
            self.plot_item = self.plot.getPlotItem()
            self.plot_item.hideAxis("left")
            self.plot_item.showAxis("right")
            self.plot_item.showAxis("bottom")
            self.plot_item.setMenuEnabled(False)
            self.plot_item.setClipToView(True)
            self.view_box.setMouseEnabled(x=True, y=True)
            root.addWidget(self.plot, 1)

            self.image_item = pg.ImageItem(axisOrder="row-major", autoDownsample=True)
            self.gap_item = pg.ImageItem(axisOrder="row-major", autoDownsample=True)
            self.plot.addItem(self.image_item)
            self.plot.addItem(self.gap_item)

            self.micro_curve = pg.PlotCurveItem()
            self.plot.addItem(self.micro_curve)
            self.trade_scatter = pg.ScatterPlotItem(pxMode=True)
            self.rpi_scatter = pg.ScatterPlotItem(pxMode=True)
            self.absorption_scatter = pg.ScatterPlotItem(pxMode=True)
            self.state_scatter = pg.ScatterPlotItem(pxMode=True)
            for item in (
                self.trade_scatter,
                self.rpi_scatter,
                self.absorption_scatter,
                self.state_scatter,
            ):
                self.plot.addItem(item)

            self.bid_segment = pg.PlotCurveItem()
            self.ask_segment = pg.PlotCurveItem()
            self.plot.addItem(self.bid_segment)
            self.plot.addItem(self.ask_segment)

            self.now_line = pg.InfiniteLine(angle=90, movable=False)
            self.plot.addItem(self.now_line)
            self.crosshair_v = pg.InfiniteLine(angle=90, movable=False)
            self.crosshair_h = pg.InfiniteLine(angle=0, movable=False)
            self.plot.addItem(self.crosshair_v, ignoreBounds=True)
            self.plot.addItem(self.crosshair_h, ignoreBounds=True)
            self.crosshair_v.hide()
            self.crosshair_h.hide()

            self.plot.scene().sigMouseMoved.connect(self._mouse_moved)
            self.plot.scene().sigMouseClicked.connect(self._mouse_clicked)
            self.view_box.manual_navigation.connect(self._manual_navigation)

            self.render_timer = QTimer(self)
            self.render_timer.setTimerType(Qt.TimerType.PreciseTimer)
            self.render_timer.setInterval(BOOKMAP_RENDER_INTERVAL_MS)
            self.render_timer.timeout.connect(self._render_if_dirty)
            self.apply_theme(theme)

        @staticmethod
        def _with_alpha(value: object, alpha: int) -> QtGui.QColor:
            color = pg.mkColor(value)
            color.setAlpha(max(0, min(255, int(alpha))))
            return color

        @staticmethod
        def _lerp_rgb(a: QtGui.QColor, b: QtGui.QColor, t: float) -> tuple[int, int, int]:
            t = max(0.0, min(1.0, float(t)))
            return (
                round(a.red() + (b.red() - a.red()) * t),
                round(a.green() + (b.green() - a.green()) * t),
                round(a.blue() + (b.blue() - a.blue()) * t),
            )

        def _heat_lut(self) -> np.ndarray:
            low = pg.mkColor(self.theme.get("bookmap_heat_low", "#10202A"))
            mid = pg.mkColor(self.theme.get("bookmap_heat_mid", "#2E5A6C"))
            high = pg.mkColor(self.theme.get("bookmap_heat_high", "#79B7C8"))
            extreme = pg.mkColor(self.theme.get("bookmap_heat_extreme", "#ECFAFF"))
            lut = np.zeros((256, 4), dtype=np.ubyte)
            stops = ((0.0, low), (0.48, mid), (0.82, high), (1.0, extreme))
            for index in range(1, 256):
                u = index / 255.0
                g = u ** BOOKMAP_NORMALIZATION_GAMMA
                left_p, left_c = stops[0]
                right_p, right_c = stops[-1]
                for candidate in range(1, len(stops)):
                    if g <= stops[candidate][0]:
                        left_p, left_c = stops[candidate - 1]
                        right_p, right_c = stops[candidate]
                        break
                local = 0.0 if right_p <= left_p else (g - left_p) / (right_p - left_p)
                r, gch, b = self._lerp_rgb(left_c, right_c, local)
                alpha = round(28 + 207 * (u ** BOOKMAP_NORMALIZATION_GAMMA))
                lut[index] = (r, gch, b, max(28, min(235, alpha)))
            return lut

        def showEvent(self, event: QtGui.QShowEvent) -> None:
            super().showEvent(event)
            self._viewport_dirty = True
            self.render_timer.setInterval(BOOKMAP_RENDER_INTERVAL_MS)
            if not self.render_timer.isActive():
                self.render_timer.start()
            self._render_if_dirty(force=True, fit_live=True)

        def hideEvent(self, event: QtGui.QHideEvent) -> None:
            self.render_timer.stop()
            super().hideEvent(event)

        def _manual_navigation(self) -> None:
            self._auto_follow = False
            self.model.set_normalization_frozen(True)
            self.live_button.setVisible(True)
            self._viewport_dirty = True

        def go_live(self) -> None:
            self._auto_follow = True
            self.model.set_normalization_frozen(False)
            self.live_button.setVisible(False)
            self._pinned = False
            self._viewport_dirty = True
            self._render_if_dirty(force=True, fit_live=True)
            self.live_requested.emit()

        def _trades_toggled(self, checked: bool) -> None:
            visible = bool(checked)
            self.trade_scatter.setVisible(visible)
            self.rpi_scatter.setVisible(visible)
            self.absorption_scatter.setVisible(visible)
            self._render_if_dirty(force=True)

        def set_order_flow_snapshot(self, payload: object) -> None:
            snapshot = getattr(payload, "snapshot", payload)
            symbol = str(getattr(snapshot, "symbol", "") or "").upper()
            if symbol and symbol != self.model.symbol:
                return
            self._order_flow_snapshot = snapshot




            if self.isVisible():
                self._viewport_dirty = True

        def set_execution_context(self, context: object | None) -> None:
            self._execution_context = context
            self._rebuild_account_items()
            self._viewport_dirty = True

        def _rebuild_account_items(self) -> None:
            for line, label, _price, _role in self._account_items:
                try:
                    self.plot.removeItem(line)
                    self.plot.removeItem(label)
                except Exception:  # noqa: BLE001
                    pass
            self._account_items.clear()
            context = self._execution_context
            if context is None:
                return
            specs: list[tuple[float, str]] = []
            for position in getattr(context, "positions", ()):
                entry = _finite_positive(getattr(position, "entry_price", 0.0))
                liquidation = _finite_positive(getattr(position, "liquidation_price", 0.0))
                if entry:
                    specs.append((entry, "entry"))
                if liquidation:
                    specs.append((liquidation, "liquidation"))
            for order in getattr(context, "orders", ()):
                price = _finite_positive(getattr(order, "price", 0.0))
                label = str(getattr(order, "label", "") or "").upper()
                role = "order"
                if "TP" in label or "TAKE" in label:
                    role = "take_profit"
                elif "SL" in label or "STOP" in label:
                    role = "stop"
                if price:
                    specs.append((price, role))
            role_key = {
                "entry": "orderflow_entry",
                "take_profit": "orderflow_take_profit",
                "stop": "orderflow_stop",
                "liquidation": "orderflow_liquidation",
                "order": "orderflow_order",
            }
            widths = {"entry": 1.25, "take_profit": 1.5, "stop": 1.5, "liquidation": 2.0, "order": 1.0}
            for price, role in specs[:16]:
                color = self.theme.get(role_key[role], self.theme.get("text", "#D0D0D0"))
                pen = pg.mkPen(
                    self._with_alpha(color, 240 if role == "liquidation" else 195),
                    width=widths[role],
                    style=Qt.PenStyle.DashLine if role == "order" else Qt.PenStyle.SolidLine,
                )
                line = pg.PlotCurveItem(pen=pen)
                tag = {"entry": "ENTRY", "take_profit": "TP", "stop": "SL", "liquidation": "LIQ", "order": "ORD"}[role]
                label = pg.TextItem(tag, color=color, anchor=(1.0, 0.5))
                label.setFont(typography_font(TextRole.ORDERBOOK_LABEL))
                self.plot.addItem(line)
                self.plot.addItem(label)
                self._account_items.append((line, label, price, role))

        def _update_account_items(self, frame: BookmapRenderFrame) -> None:
            if not self._account_items:
                return
            x_range = self.view_box.viewRange()[0]
            visible_span = max(1.0, float(x_range[1] - x_range[0]))
            segment = min(8.0, visible_span * 0.15)
            start_x = frame.newest_time_s - segment
            label_x = frame.newest_time_s - max(0.12, segment * 0.03)
            for line, label, price, _role in self._account_items:
                line.setData([start_x, frame.newest_time_s], [price, price])
                label.setPos(label_x, price)

        def apply_theme(self, theme: dict[str, Any]) -> None:
            self.theme = dict(theme)
            background = str(theme.get("bookmap_background", theme.get("orderbook_bg", theme.get("bg", "#000000"))))
            panel = str(theme.get("panel", background))
            text = str(theme.get("text", "#D0D0D0"))
            muted = str(theme.get("muted", "#808080"))
            border = str(theme.get("border", "#202020"))
            self.plot.setBackground(background)
            self.setStyleSheet(f"background:{background};")
            self.toolbar.setStyleSheet(f"background:{panel};border-bottom:1px solid {border};")
            self.inspector.setStyleSheet(f"background:{panel};border-bottom:1px solid {border};")
            self.status_label.setStyleSheet(f"color:{muted};background:transparent;")
            self.inspect_primary.setStyleSheet(f"color:{text};background:transparent;")
            self.inspect_secondary.setStyleSheet(f"color:{muted};background:transparent;")
            for button in (self.trades_button, self.live_button):
                button.setStyleSheet(
                    f"QToolButton{{color:{muted};background:transparent;border:1px solid {border};padding:0 5px;}}"
                    f"QToolButton:hover,QToolButton:checked{{color:{text};background:{theme.get('control_hover', panel)};}}"
                )

            self.image_item.setLookupTable(self._heat_lut())
            invalid = pg.mkColor(theme.get("bookmap_invalid", theme.get("orderflow_depletion", theme.get("red", "#E60028"))))
            gap_lut = np.zeros((2, 4), dtype=np.ubyte)
            gap_lut[1] = (invalid.red(), invalid.green(), invalid.blue(), 42)
            self.gap_item.setLookupTable(gap_lut)
            self.gap_item.setLevels((0.0, 1.0))

            self.micro_curve.setPen(pg.mkPen(self._with_alpha(theme.get("bookmap_price_trace", theme.get("book_mid", text)), 218), width=1.25))
            self.bid_segment.setPen(pg.mkPen(self._with_alpha(theme.get("book_bid", theme.get("green", "#009F52")), 225), width=1.25))
            self.ask_segment.setPen(pg.mkPen(self._with_alpha(theme.get("book_ask", theme.get("red", "#E60028")), 225), width=1.25))
            self.now_line.setPen(pg.mkPen(self._with_alpha(theme.get("bookmap_now", muted), 105), width=1.0))
            cross_pen = pg.mkPen(self._with_alpha(theme.get("bookmap_crosshair", muted), 100), width=1.0, style=Qt.PenStyle.DotLine)
            self.crosshair_v.setPen(cross_pen)
            self.crosshair_h.setPen(cross_pen)
            axis_pen = pg.mkPen(theme.get("bookmap_axis", muted))
            for axis in (self.price_axis, self.time_axis):
                axis.setPen(axis_pen)
                axis.setTextPen(axis_pen)
                axis.setStyle(tickFont=typography_font(TextRole.CHART_AXIS))
            self._rebuild_account_items()
            self._last_revision = -1

        def _set_status(self, frame: BookmapRenderFrame | None) -> None:
            if frame is None:
                self.status_label.setText("● SYNCING · 60s")
                return
            current_valid = bool(frame.live and frame.book_valid and frame.validity.size and bool(frame.validity[-1]))
            if current_valid:
                state = "LIVE" if self._auto_follow else "MANUAL"
            else:
                state = str(frame.validity_reason or "SYNCING").upper()
            if self._auto_follow:
                span_text = "60s"
            else:
                x_range = self.view_box.viewRange()[0]
                visible_span_s = max(0.0, float(x_range[1] - x_range[0]))
                rounded_s = max(1, int(round(visible_span_s)))
                if rounded_s < 90:
                    span_text = f"{rounded_s}s"
                else:
                    minutes, seconds = divmod(rounded_s, 60)
                    span_text = f"{minutes}m {seconds:02d}s"
            step = _format_bookmap_price(frame.price_step, frame.price_step)
            self.status_label.setText(f"● {state} · {span_text} · {step}/ROW · 100ms")

        def _set_trade_scatter(self, points: BookmapTradePoints) -> None:
            if points.x.size == 0 or not self.trades_button.isChecked():
                self.trade_scatter.setData(x=[], y=[])
                self.rpi_scatter.setData(x=[], y=[])
                self.absorption_scatter.setData(x=[], y=[])
                return
            brushes: list[QtGui.QBrush] = []
            pens: list[QtGui.QPen] = []
            muted = self.theme.get("muted", "#808080")
            buy_color = self.theme.get("bookmap_trade_buy", self.theme.get("green", "#009F52"))
            sell_color = self.theme.get("bookmap_trade_sell", self.theme.get("red", "#E60028"))
            for imbalance in points.imbalance:
                magnitude = abs(float(imbalance))
                alpha = int(max(140, min(230, round(140 + 90 * magnitude))))
                base = muted if magnitude < 0.15 else (buy_color if imbalance > 0.0 else sell_color)
                color = self._with_alpha(base, alpha)
                brushes.append(pg.mkBrush(color))
                pens.append(pg.mkPen(color, width=0.45))
            self.trade_scatter.setData(
                x=points.x,
                y=points.y,
                size=points.size,
                brush=brushes,
                pen=pens,
                pxMode=True,
            )
            mask = points.rpi > 0.0
            if np.any(mask):
                rpi_color = self.theme.get("orderflow_rpi", self.theme.get("purple", "#8072B3"))
                rpi_pens = [
                    pg.mkPen(self._with_alpha(rpi_color, 220), width=2.25 if frac >= 0.5 else 1.5)
                    for frac in points.rpi_fraction[mask]
                ]
                self.rpi_scatter.setData(
                    x=points.x[mask],
                    y=points.y[mask],
                    size=points.size[mask] + 2.0,
                    pen=rpi_pens,
                    brush=None,
                    pxMode=True,
                )
            else:
                self.rpi_scatter.setData(x=[], y=[])

        def _render_current_state_cues(
            self,
            frame: BookmapRenderFrame | None,
            trade_points: BookmapTradePoints | None,
        ) -> None:
            snapshot = self._order_flow_snapshot
            if frame is None or snapshot is None or str(getattr(snapshot, "symbol", "") or "").upper() != self.model.symbol:
                self.state_scatter.setData(x=[], y=[])
                self.absorption_scatter.setData(x=[], y=[])
                return
            levels = list(getattr(snapshot, "bid_levels", ())[:48]) + list(getattr(snapshot, "ask_levels", ())[:48])
            cue_x: list[float] = []
            cue_y: list[float] = []
            cue_brush: list[QtGui.QBrush] = []
            cue_pen: list[QtGui.QPen] = []
            role_for_state = {
                "STACKING": "orderflow_stacking",
                "PULLING": "orderflow_pulling",
                "DEPLETING": "orderflow_depletion",
            }
            absorbing_prices: list[float] = []
            for level in levels:
                state = str(getattr(level, "state", "NORMAL") or "NORMAL").upper()
                price = _finite_positive(getattr(level, "price", 0.0))
                if price <= 0.0:
                    continue
                if state == "ABSORBING":
                    absorbing_prices.append(price)
                    continue
                if state not in role_for_state:
                    continue
                intensity = max(float(getattr(level, "liquidity_intensity", 0.0) or 0.0), float(getattr(level, "trade_intensity", 0.0) or 0.0))
                persistence = float(getattr(level, "persistence_ratio", 0.0) or 0.0)
                if intensity < 0.45 and persistence < 0.55:
                    continue
                color = self.theme.get(role_for_state[state], self.theme.get("muted", "#808080"))
                cue_x.append(frame.newest_time_s)
                cue_y.append(price)
                cue_brush.append(pg.mkBrush(self._with_alpha(color, 220)))
                cue_pen.append(pg.mkPen(self._with_alpha(color, 235), width=0.6))
                if len(cue_x) >= 10:
                    break
            self.state_scatter.setData(
                x=cue_x,
                y=cue_y,
                size=5,
                symbol="s",
                brush=cue_brush,
                pen=cue_pen,
                pxMode=True,
            ) if cue_x else self.state_scatter.setData(x=[], y=[])

            if trade_points is None or not absorbing_prices or trade_points.x.size == 0:
                self.absorption_scatter.setData(x=[], y=[])
                return
            mask = np.zeros(trade_points.x.shape, dtype=np.bool_)
            recent = trade_points.x >= frame.newest_time_s - 2.0
            for price in absorbing_prices[:12]:
                mask |= recent & (np.abs(trade_points.y - price) <= max(self.model.price_step * 0.75, 1e-12))
            if np.any(mask):
                color = self.theme.get("orderflow_absorption", self.theme.get("purple", "#9A78C7"))
                self.absorption_scatter.setData(
                    x=trade_points.x[mask],
                    y=trade_points.y[mask],
                    size=trade_points.size[mask] + 4.0,
                    pen=pg.mkPen(self._with_alpha(color, 220), width=2.0),
                    brush=None,
                    pxMode=True,
                )
            else:
                self.absorption_scatter.setData(x=[], y=[])

        def _render_if_dirty(self, *, force: bool = False, fit_live: bool = False) -> None:
            if not self.isVisible():
                return
            if not force and not self._viewport_dirty and self.model.revision == self._last_revision:
                return
            started = time.perf_counter()
            frame = self.model.render_frame(
                window_ms=BOOKMAP_DEFAULT_VISIBLE_MS if self._auto_follow else None
            )
            self._last_revision = self.model.revision
            self._last_frame = frame
            self._viewport_dirty = False
            self._set_status(frame)
            if frame is None:
                self.image_item.hide()
                self.gap_item.hide()
                self.micro_curve.setData([], [])
                self.bid_segment.setData([], [])
                self.ask_segment.setData([], [])
                self.trade_scatter.setData(x=[], y=[])
                self.rpi_scatter.setData(x=[], y=[])
                self.absorption_scatter.setData(x=[], y=[])
                self.state_scatter.setData(x=[], y=[])
                self._last_trade_points = None
                return

            self.price_axis.set_step(frame.price_step)
            axis_font = typography_font(TextRole.CHART_AXIS)
            axis_sample = frame.best_ask or frame.best_bid or (frame.price_origin + frame.price_step * self.model.price_rows * 0.5)
            axis_text = _format_bookmap_price(axis_sample, frame.price_step)
            axis_width = QtGui.QFontMetrics(axis_font).horizontalAdvance(axis_text) + 14
            self.price_axis.setWidth(max(72, min(112, axis_width)))

            width_s = max(
                self.model.capture_interval_ms / 1000.0,
                frame.newest_time_s - frame.oldest_time_s + self.model.capture_interval_ms / 1000.0,
            )
            rect = QtCore.QRectF(
                frame.oldest_time_s,
                frame.price_origin,
                width_s,
                frame.price_step * self.model.price_rows,
            )
            self.image_item.show()
            self.gap_item.show()
            self.image_item.setImage(
                frame.image,
                autoLevels=False,
                levels=(
                    max(0.0, frame.normalization_low),
                    max(frame.normalization_low + 1e-6, frame.normalization_high),
                ),
            )
            self.image_item.setRect(rect)
            gap = (~frame.validity).astype(np.float32, copy=False)[np.newaxis, :]
            self.gap_item.setImage(gap, autoLevels=False, levels=(0.0, 1.0))
            self.gap_item.setRect(rect)

            trace_x, trace_y = self.model.price_trace_points(start_time_s=frame.oldest_time_s)
            self.micro_curve.setData(trace_x, trace_y, connect="finite")
            self.now_line.setPos(frame.newest_time_s)

            x_range = self.view_box.viewRange()[0]
            visible_span = BOOKMAP_DEFAULT_VISIBLE_MS / 1000.0 if self._auto_follow else max(1.0, float(x_range[1] - x_range[0]))
            pixel_width = max(1, int(self.plot.viewport().width()))
            bbo_segment = max(0.25, min(4.0, visible_span * 28.0 / pixel_width))
            current_valid = bool(frame.live and frame.book_valid and frame.validity.size and bool(frame.validity[-1]))
            if current_valid and frame.best_bid > 0.0:
                self.bid_segment.setData([frame.newest_time_s - bbo_segment, frame.newest_time_s], [frame.best_bid, frame.best_bid])
            else:
                self.bid_segment.setData([], [])
            if current_valid and frame.best_ask > 0.0:
                self.ask_segment.setData([frame.newest_time_s - bbo_segment, frame.newest_time_s], [frame.best_ask, frame.best_ask])
            else:
                self.ask_segment.setData([], [])

            trade_points: BookmapTradePoints | None = None
            if self.trades_button.isChecked():
                trade_points = self.model.trade_points(
                    start_time_s=frame.oldest_time_s,
                    end_time_s=frame.newest_time_s,
                )
                self._last_trade_points = trade_points
                self._set_trade_scatter(trade_points)
            self._render_current_state_cues(frame, trade_points or self._last_trade_points)
            self._update_account_items(frame)

            if self._auto_follow or fit_live:
                x_span = min(BOOKMAP_DEFAULT_VISIBLE_MS / 1000.0, width_s)
                self.view_box.setXRange(frame.newest_time_s - x_span, frame.newest_time_s, padding=0.0)
                current = frame.microprice or ((frame.best_bid + frame.best_ask) * 0.5)
                if current > 0.0:
                    target_span = max(current * 0.0040, (self.model.tick_size or frame.price_step) * 200.0)
                    rows = int(max(256, min(640, math.ceil(target_span / max(frame.price_step, 1e-12)))))
                    span = rows * frame.price_step
                    current_range = self.view_box.viewRange()[1]
                    current_span = float(current_range[1] - current_range[0])
                    needs = (
                        fit_live
                        or current_span <= 0.0
                        or not math.isfinite(current_span)
                        or current < current_range[0] + current_span * 0.15
                        or current > current_range[1] - current_span * 0.15
                    )
                    if needs:
                        self.view_box.setYRange(current - span * 0.5, current + span * 0.5, padding=0.0)


        def _current_state_for_price(self, price: float) -> str:
            snapshot = self._order_flow_snapshot
            if snapshot is None:
                return ""
            reference_step = max(self.model.price_step, self.model.tick_size, 1e-12)
            best_state = ""
            for level in list(getattr(snapshot, "bid_levels", ())[:48]) + list(getattr(snapshot, "ask_levels", ())[:48]):
                level_price = _finite_positive(getattr(level, "price", 0.0))
                if abs(level_price - price) <= reference_step * 0.75:
                    state = str(getattr(level, "state", "NORMAL") or "NORMAL").upper()
                    if state != "NORMAL":
                        best_state = state
                        break
            return best_state

        def _update_inspector(self, x: float, y: float) -> None:
            info = self.model.inspect(x, y)
            if not info:
                self.inspect_primary.setText("No retained liquidity at cursor")
                self.inspect_secondary.setText("")
                return
            stamp = datetime.fromtimestamp(int(info["timestamp_ms"]) / 1000.0, tz=timezone.utc)
            price = _format_bookmap_price(float(info["price"]), self.model.price_step)
            if not bool(info["valid"]):
                self.inspect_primary.setText(f"{stamp:%H:%M:%S.%f}"[:-3] + f" · {price} · GAP / INVALID BOOK")
                self.inspect_secondary.setText("Historical continuity is intentionally broken here")
                return
            resting = float(info["resting_notional"])
            peak = float(info.get("peak_notional", 0.0))
            persist = float(info["persistence_seconds"])
            buy = float(info["buy_notional"])
            sell = float(info["sell_notional"])
            net = buy - sell
            rpi = float(info["rpi_notional"])
            state = self._current_state_for_price(float(info["price"])) if self._last_frame and x >= self._last_frame.newest_time_s - 1.0 else ""
            self.inspect_primary.setText(
                f"{stamp:%H:%M:%S.%f}"[:-3]
                + f" · {price} · REST {human_number(resting, money=True)}"
                + f" · PERSIST {persist:.1f}s · PEAK {human_number(peak, money=True)}"
            )
            secondary = (
                f"BUY {human_number(buy, money=True)} · SELL {human_number(sell, money=True)}"
                f" · Δ {human_number(net, money=True)}"
                f" · DIST {float(info.get('distance_ticks', 0.0)):+.1f}t / {float(info.get('distance_bps', 0.0)):+.2f}bp"
            )
            if rpi > 0.0:
                secondary += f" · RPI {human_number(rpi, money=True)}"
            if state:
                secondary += f" · {state}"
            self.inspect_secondary.setText(secondary)

        def _mouse_moved(self, event: Any) -> None:
            if self._pinned:
                return
            try:
                scene_pos = event[0] if isinstance(event, tuple) else event
            except (TypeError, IndexError):
                return
            if not self.plot_item.sceneBoundingRect().contains(scene_pos):
                self.crosshair_v.hide()
                self.crosshair_h.hide()
                self.inspect_primary.setText("Hover to inspect displayed liquidity")
                self.inspect_secondary.setText("")
                return
            point = self.view_box.mapSceneToView(scene_pos)
            x, y = float(point.x()), float(point.y())
            self.crosshair_v.setPos(x)
            self.crosshair_h.setPos(y)
            self.crosshair_v.show()
            self.crosshair_h.show()
            self._update_inspector(x, y)

        def _mouse_clicked(self, event: Any) -> None:
            try:
                scene_pos = event.scenePos()
            except Exception:  # noqa: BLE001
                return
            if not self.plot_item.sceneBoundingRect().contains(scene_pos):
                return
            if getattr(event, "double", lambda: False)():
                self.go_live()
                event.accept()
                return
            if event.button() != Qt.MouseButton.LeftButton:
                return
            if self._pinned:
                self._pinned = False
                self.crosshair_v.hide()
                self.crosshair_h.hide()
                self.inspect_primary.setText("Hover to inspect displayed liquidity")
                self.inspect_secondary.setText("")
            else:
                point = self.view_box.mapSceneToView(scene_pos)
                self._pinned_x, self._pinned_y = float(point.x()), float(point.y())
                self._pinned = True
                self.crosshair_v.setPos(self._pinned_x)
                self.crosshair_h.setPos(self._pinned_y)
                self.crosshair_v.show()
                self.crosshair_h.show()
                self._update_inspector(self._pinned_x, self._pinned_y)
            event.accept()

        def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
            if event.key() == Qt.Key.Key_Escape and self._pinned:
                self._pinned = False
                self.crosshair_v.hide()
                self.crosshair_h.hide()
                self.inspect_primary.setText("Hover to inspect displayed liquidity")
                self.inspect_secondary.setText("")
                event.accept()
                return
            super().keyPressEvent(event)

else:

    class BookmapWidget:  # type: ignore[no-redef]
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("BookmapWidget requires PySide6 and pyqtgraph")
