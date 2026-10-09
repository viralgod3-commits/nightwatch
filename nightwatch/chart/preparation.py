"""Headless chart preparation. No Qt objects or graphics resources cross this boundary.

Closed matrix rows are immutable after publication. A producer may replace its
live row; workers only receive the closed prefix and a copy of that one row.
Prepared batches include upload bytes, bounds and range indexes so a frame
commit never sorts history, builds LOD, packs vertices or scans all candles.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from bisect import bisect_left
import math
import numpy as np

from ..models import CandlePages, chart_y_array, _coerce_candle_matrix, _candle_matrix_from_objects, safe_float
from ..constants import INTERVAL_SECONDS, MAX_CHART_CANDLES


def candle_matrix_capacity(size: int) -> int:
    """Bound backing storage to retained rows plus 1K–8K append slots.

    Exhaustion uses the existing asynchronous snapshot/compaction path; never
    resize published arrays that analysis workers may still be reading.
    """
    size = max(0, min(int(size), MAX_CHART_CANDLES))
    spare = min(8192, max(1024, size // 16))
    return size + spare


class RangeExtrema:
    """Immutable segment index: exact range extrema in O(log N), including gaps."""
    def __init__(self, low, high):
        self.count = len(low)
        self.base = 1 << max(0, (max(1, self.count)-1).bit_length())
        self.low = np.full(self.base*2, np.inf)
        self.high = np.full(self.base*2, -np.inf)
        self.low[self.base:self.base+self.count] = np.where(np.isfinite(low), low, np.inf)
        self.high[self.base:self.base+self.count] = np.where(np.isfinite(high), high, -np.inf)
        width = self.base
        while width > 1:
            begin = width//2
            self.low[begin:width] = np.minimum(self.low[width:2*width:2], self.low[width+1:2*width:2])
            self.high[begin:width] = np.maximum(self.high[width:2*width:2], self.high[width+1:2*width:2])
            width = begin
        self.low.flags.writeable = self.high.flags.writeable = False

    def query(self, first, last):
        left = self.base + max(0, int(first))
        right = self.base + min(self.count, int(last))
        low, high = math.inf, -math.inf
        while left < right:
            if left & 1:
                low, high = min(low, self.low[left]), max(high, self.high[left])
                left += 1
            if right & 1:
                right -= 1
                low, high = min(low, self.low[right]), max(high, self.high[right])
            left //= 2
            right //= 2
        return float(low), float(high)


@dataclass(frozen=True)
class PreparedBars:
    data: np.ndarray
    bounds: tuple[float, float, float, float]
    payload: bytes
    origin: tuple[float, float]
    extrema: RangeExtrema
    times: np.ndarray
    max_slot_width: float
    times_sorted: bool

    def screen_slice(self, left, right, scale, offset, pixel_margin=2.0):
        """Conservatively select X-visible rows without scanning resident history.

        Keep a full slot on either edge (including irregular LOD slots), plus
        pixel-space wick/glow padding. Unordered input retains the full batch.
        The contiguous timestamp index is built with the immutable payload;
        searching a strided data column can otherwise copy it on every frame.
        """
        count = len(self.data)
        if (not self.times_sorted or not count or abs(scale) < 1e-18
                or not all(math.isfinite(v) for v in (left, right, scale, offset, pixel_margin))):
            return 0, count
        x0, x1 = sorted(((left - offset) / scale, (right - offset) / scale))
        margin = self.max_slot_width + max(0.0, pixel_margin) / abs(scale)
        first = int(np.searchsorted(self.times, x0 - margin, side="left"))
        last = int(np.searchsorted(self.times, x1 + margin, side="right"))
        return first, last


def prepare_bars(candles, slots=None, logarithmic=False, volume=False):
    source = _coerce_candle_matrix(candles)
    if slots is None or np.isscalar(slots):
        widths = np.full(len(source), 1.0 if slots is None else float(slots))
    else:
        widths = np.asarray(slots, dtype=np.float64).reshape(-1)
    count = min(len(source), len(widths))
    source, widths = source[:count], widths[:count]
    valid = np.isfinite(source[:, :6]).all(axis=1) & np.isfinite(widths) & (widths > 0)
    valid &= (source[:, 5] > 0) if volume else (np.min(source[:, 1:5], axis=1) > 0)
    source, widths = source[valid], widths[valid]
    data = np.empty((len(source), 7), dtype=np.float64)
    data[:, 0] = source[:, 0]
    if volume:
        data[:, 1] = data[:, 3] = 0
        data[:, 2] = data[:, 4] = source[:, 5]
    else:
        data[:, 1:5] = chart_y_array(source[:, [1, 4, 3, 2]], logarithmic)
    data[:, 5] = source[:, 4] >= source[:, 1]
    data[:, 6] = widths
    if not len(data):
        bounds, origin = (0., 0., 0., 0.), (0., 0.)
        packed = np.empty((0, 8), dtype=np.float32)
    else:
        left = float(np.min(data[:, 0]-widths*.5))
        right = float(np.max(data[:, 0]+widths*.5))
        low, high = float(np.min(data[:, 3:5])), float(np.max(data[:, 3:5]))
        bounds = (left, low, right-left, max(high-low, abs(high)*1e-12, 1e-12))
        origin = (float(data[0, 0]), float(data[0, 1]))
        packed = np.zeros((len(data), 8), dtype=np.float32)
        packed[:, 0] = data[:, 0]-origin[0]
        packed[:, 1:5] = data[:, 1:5]-origin[1]
        packed[:, 5:7] = data[:, 5:7]
    data.flags.writeable = False
    times = np.array(data[:, 0], copy=True)
    times.flags.writeable = False
    return PreparedBars(
        data, bounds, packed.tobytes(), origin, RangeExtrema(data[:, 3], data[:, 4]),
        times, float(np.max(widths)) if len(widths) else 0.0,
        bool(np.all(times[1:] >= times[:-1])),
    )


@dataclass(frozen=True)
class PreparedWindow:
    key: tuple
    window: tuple[int, int, int]
    candles: PreparedBars
    volume: PreparedBars
    closed_time: float | None


def _aggregate_components(source, ends, stride, seconds):
    """Reduce raw rows or earlier aggregates, retaining actual edge timestamps."""
    buckets = np.floor(source[:, 0] / (stride * seconds))
    starts = np.r_[0, np.flatnonzero(np.diff(buckets)) + 1]
    stops = np.r_[starts[1:] - 1, len(source) - 1]
    matrix = np.empty((len(starts), 7), dtype=np.float64)
    matrix[:, 0:2] = source[starts, 0:2]
    matrix[:, 2] = np.maximum.reduceat(source[:, 2], starts)
    matrix[:, 3] = np.minimum.reduceat(source[:, 3], starts)
    matrix[:, 4] = source[stops, 4]
    matrix[:, 5] = np.add.reduceat(source[:, 5], starts)
    matrix[:, 6] = np.add.reduceat(source[:, 6], starts)
    return matrix, ends[stops]


class CandleLodIndex:
    """Immutable multiresolution OHLCV index, built on a preparation worker.

    Start at 16 candles: a contiguous 250K-row history needs about 2 MB for
    all levels together. Zoom/pan preparation reads aggregated rows; live
    updates combine cached closed buckets with just the unpublished tail.
    Actual first/last times preserve gaps and incomplete exchange-time buckets.
    """
    def __init__(self, closed, seconds):
        self.seconds = float(seconds)
        self.levels = {}
        rows = closed
        ends = closed[:, 0]
        stride = 16
        while len(rows) and stride <= 2 * MAX_CHART_CANDLES:
            # Sparse histories can have no merges at this scale. Share the
            # previous immutable level instead of retaining redundant copies.
            reduced, last_times = _aggregate_components(rows, ends, stride, self.seconds)
            if len(reduced) < len(rows) or not self.levels:
                rows, ends = reduced, last_times
                rows.flags.writeable = ends.flags.writeable = False
            self.levels[stride] = rows, ends
            stride *= 2

    def components(self, source, stride, seconds):
        level = self.levels.get(int(stride)) if seconds == self.seconds else None
        if level is None or not len(source):
            return source, source[:, 0]
        rows, ends = level
        first = int(np.searchsorted(rows[:, 0], source[0, 0], side="left"))
        last = int(np.searchsorted(ends, source[-1, 0], side="right"))
        if last <= first:
            return source, source[:, 0]
        # Edge rows may have been evicted, newly prepended, or appended since
        # publication. Never include a cached bucket outside the requested data.
        left = int(np.searchsorted(source[:, 0], rows[first, 0], side="left"))
        right = int(np.searchsorted(source[:, 0], ends[last - 1], side="right"))
        if left == 0 and right == len(source):
            return rows[first:last], ends[first:last]
        return (
            np.concatenate((source[:left], rows[first:last], source[right:])),
            np.concatenate((source[:left, 0], ends[first:last], source[right:, 0])),
        )


def _aggregate_lod_rows(source, stride, seconds, *, full_slot_width=False, lod_index=None):
    """Aggregate rows on the exchange-time LOD grid shared by history and live.

    ``full_slot_width`` is used by the mutable live tail: an incomplete current
    bucket keeps the same horizontal footprint as neighboring committed LOD bars
    while its OHLCV continues to evolve.  History only publishes complete trailing
    buckets, so the two VBOs never own the same bucket at once.
    """
    source = _coerce_candle_matrix(source)
    stride = max(1, int(stride))
    seconds = float(seconds)
    if stride <= 1 or not len(source):
        return source, seconds

    bucket_seconds = stride * seconds
    ends = source[:, 0]
    if lod_index is not None:
        source, ends = lod_index.components(source, stride, seconds)
    matrix, last_times = _aggregate_components(source, ends, stride, seconds)
    if full_slot_width:
        matrix[:, 0] = np.floor(matrix[:, 0] / bucket_seconds) * bucket_seconds + (stride - 1) * seconds * 0.5
        widths = np.full(len(matrix), bucket_seconds, dtype=np.float64)
    else:
        widths = last_times - matrix[:, 0] + seconds
        matrix[:, 0] = (matrix[:, 0] + last_times) * 0.5
    return matrix, widths


def prepare_live_tail(candles, stride, seconds, logarithmic, lod_index=None):
    """Prepare the uncommitted trailing LOD buckets for candle + volume VBOs."""
    matrix, widths = _aggregate_lod_rows(
        candles,
        stride,
        seconds,
        full_slot_width=int(stride) > 1,
        lod_index=lod_index,
    )
    return (
        prepare_bars(matrix, widths, logarithmic),
        prepare_bars(matrix, widths, volume=True),
    )


def prepare_window(key, closed, window, seconds, logarithmic, lod_index=None):
    first, last, stride = window
    first, last = max(0, first), min(len(closed), last)
    if stride > 1 and first < last:


        bucket_seconds = stride * seconds
        start_time = math.floor(closed[first, 0] / bucket_seconds) * bucket_seconds
        end_time = (math.floor(closed[last-1, 0] / bucket_seconds) + 1) * bucket_seconds
        first = int(np.searchsorted(closed[:, 0], start_time))
        last = int(np.searchsorted(closed[:, 0], end_time))





    source = closed[first:last]
    history_source = source
    if stride > 1 and len(source):
        bucket_seconds = stride * seconds
        final_bucket_start = math.floor(source[-1, 0] / bucket_seconds) * bucket_seconds
        expected_final_time = final_bucket_start + (stride - 1) * seconds
        if source[-1, 0] < expected_final_time - max(1e-9, abs(seconds) * 1e-9):
            trailing_start = int(np.searchsorted(source[:, 0], final_bucket_start, side="left"))
            history_source = source[:trailing_start]

    matrix, widths = _aggregate_lod_rows(history_source, stride, seconds, lod_index=lod_index)


    committed_closed_time = float(history_source[-1, 0]) if len(history_source) else None
    return PreparedWindow(key, (first, last, stride), prepare_bars(matrix, widths, logarithmic),
                          prepare_bars(matrix, widths, volume=True), committed_closed_time)


def prepare_snapshot(payload, current=()):
    source = payload.get("candles", ())
    candles = None
    if (payload.get('_canonical_candles') or payload.get('_storage_roll')) and isinstance(source, CandlePages):
        # Cached histories are already sorted/unique. Preserve their native
        # pages and patch just the live tail instead of materializing all rows.
        updates, suffix = {}, []
        tail = {c.time: c for c in current}
        current = tuple(tail[stamp] for stamp in sorted(tail))
        for candle in current:
            if not source or candle.time > source[-1].time:
                suffix.append(candle)
            else:
                index = bisect_left(source, candle.time, key=lambda c: c.time)
                if index >= len(source) or source[index].time != candle.time:
                    break
                updates[index] = candle
        else:
            candles = source.updated(updates).extended(suffix)
            if len(candles) > MAX_CHART_CANDLES:
                candles = candles.snapshot(len(candles) - MAX_CHART_CANDLES)
    if candles is None:
        rows = {c.time: c for c in source}
        rows.update((c.time, c) for c in current)
        candles = CandlePages(sorted(rows.values(), key=lambda c: c.time)[-MAX_CHART_CANDLES:])
    matrix = _candle_matrix_from_objects(candles)
    storage = np.empty((candle_matrix_capacity(len(matrix)), 7), dtype=np.float64)
    storage[:len(matrix)] = matrix
    result = dict(payload)
    result["candles"] = candles
    result["_storage"] = storage
    result["_times"] = CandlePages.from_matrix(matrix[:, :1], column=0)

    result["_extrema"] = RangeExtrema(matrix[:-1, 3], matrix[:-1, 2])
    result["_lod_index"] = CandleLodIndex(matrix[:-1], INTERVAL_SECONDS[payload["interval"]])
    return result


def decimate_series(times, values, pixel_width):
    """Keep endpoints, extrema and NaN breaks within a physical-pixel budget."""
    count = min(len(times), len(values))
    budget = max(512, min(6000, int(max(320., pixel_width)*3)))
    if count <= budget:
        return times[:count], values[:count], False
    edges = np.linspace(0, count, max(1, (budget-2)//5)+1, dtype=np.int64)
    chosen = [0, count-1]
    for start, end in zip(edges[:-1], edges[1:]):
        segment = values[start:end]
        finite = np.flatnonzero(np.isfinite(segment))
        chosen.extend((int(start), int(end-1)))
        if finite.size:
            chosen.extend((int(start+finite[np.argmin(segment[finite])]), int(start+finite[np.argmax(segment[finite])])))
        missing = np.flatnonzero(~np.isfinite(segment))
        if missing.size:
            chosen.append(int(start+missing[0]))
    selected = np.asarray(sorted(set(chosen)), dtype=np.int64)
    return times[selected], values[selected], True


def funding_indices(times, rates, x0, x1, width):
    guard = max(x1-x0, 1.)*.12
    left, right = int(np.searchsorted(times, x0-guard)), int(np.searchsorted(times, x1+guard, side="right"))
    budget = max(24, min(6000, int(max(width, 48.)*.5)))
    if right-left <= budget:
        return np.arange(left, right, dtype=np.int64)
    chosen = []
    edges = np.linspace(left, right, max(1, budget//2)+1, dtype=np.int64)
    for start, end in zip(edges[:-1], edges[1:]):
        values = rates[start:end]
        if len(values):
            chosen.extend((int(start+np.argmin(values)), int(start+np.argmax(values))))
    return np.asarray(sorted(set(chosen)), dtype=np.int64)


def prepare_study(kind, rows, smoothing, x0, x1, width):
    """Prepare OI/funding arrays, exact scaling and display decimation together."""
    smoothing = max(1, int(smoothing))
    if kind == "oi":
        times = np.fromiter((safe_float(r.get("timestamp"))/1000 for r in rows), dtype=float)
        values = np.fromiter((safe_float(r.get("sumOpenInterestValue"), math.nan) for r in rows), dtype=float)
        values[~np.isfinite(values) | (values <= 0)] = np.nan
        if smoothing > len(values):
            values[:] = np.nan
        elif smoothing > 1:
            values = np.r_[np.full(smoothing-1, np.nan), np.convolve(values, np.ones(smoothing)/smoothing, mode="valid")]
        visible = values[np.searchsorted(times, x0):np.searchsorted(times, x1, side="right")]
        finite = visible[np.isfinite(visible)]
        if not len(finite):
            finite = values[np.isfinite(values)]
        extent = (float(finite.min()), float(finite.max())) if len(finite) else None
        shown_x, shown_y, _ = decimate_series(times, values, width)
        return {"curve": (shown_x, shown_y), "extent": extent}
    times = np.fromiter((safe_float(r.get("fundingTime"))/1000 for r in rows), dtype=float)
    rates = np.fromiter((safe_float(r.get("fundingRate"))*100 for r in rows), dtype=float)
    valid = np.isfinite(times) & np.isfinite(rates) & (times > 0)
    times, rates = times[valid], rates[valid]
    order = np.argsort(times, kind="stable")
    times, rates = times[order], rates[order]
    smoothed = np.empty_like(rates)
    previous = math.nan
    alpha = 2/(smoothing+1)
    for i, value in enumerate(rates):
        previous = value if not math.isfinite(previous) else alpha*value+(1-alpha)*previous
        smoothed[i] = previous
    a, b = np.searchsorted(times, x0), np.searchsorted(times, x1, side="right")
    raw, smooth = (rates[a:b], smoothed[a:b]) if b > a else (rates, smoothed)
    extent = (min(float(raw.min()), float(smooth.min()), 0.), max(float(raw.max()), float(smooth.max()), 0.)) if len(raw) else None
    selected = funding_indices(times, rates, x0, x1, width)
    positive = rates[selected] >= 0
    pos, neg = selected[positive], selected[~positive]
    spacing = max(1., float(np.median(np.diff(times)))*.6) if len(times)>1 else 5*3600.
    shown_x, shown_y, _ = decimate_series(times, smoothed, width)
    return {"curve": (shown_x, shown_y), "positive": (times[pos], rates[pos]),
            "negative": (times[neg], rates[neg]), "bar_width": spacing, "extent": extent}


def prepare_storage_recovery(payload):
    """Rebuild canonical rolling storage in a worker after normal prep failed."""
    candles = payload["candles"]
    matrix = _candle_matrix_from_objects(candles)
    storage = np.empty((candle_matrix_capacity(len(matrix)), 7), dtype=np.float64)
    storage[:len(matrix)] = matrix
    return {**payload, "candles": candles, "_storage": storage,
            "_times": CandlePages.from_matrix(matrix[:, :1], column=0),
            "_extrema": RangeExtrema(matrix[:-1, 3], matrix[:-1, 2]),
            "_lod_index": CandleLodIndex(matrix[:-1], INTERVAL_SECONDS[payload["interval"]])}


def parse_liquidation(event, fallback_time_ms=0):
    order = event.get("o") or {}
    if not order:
        return None
    price = safe_float(order.get("ap"))
    if price <= 0:
        price = safe_float(order.get("p"))
    quantity = safe_float(order.get("z"))
    if quantity <= 0:
        quantity = safe_float(order.get("l") or order.get("q"))
    if price <= 0 or quantity <= 0:
        return None
    timestamp_ms = int(
        safe_float(order.get("T") or event.get("E") or fallback_time_ms)
    )
    if timestamp_ms <= 0:
        return None
    return {
        "time": timestamp_ms / 1000.0,
        "price": price,
        "quantity": quantity,
        "notional": price * quantity,
        "side": str(order.get("S") or "SELL").upper(),
    }


def prepare_event_history(kind, current, batches, minimum):
    if kind == "funding":
        by_time = {int(safe_float(row.get("fundingTime"))): row for row in current
                   if int(safe_float(row.get("fundingTime"))) > 0}
        for batch in batches:
            for row in batch:
                stamp = int(safe_float(row.get("fundingTime")))
                if stamp > 0:
                    by_time[stamp] = dict(row)
        return [by_time[stamp] for stamp in sorted(by_time)][-5000:]
    merged = {(item["time"], item["side"], item["price"], item["quantity"]): item
              for item in current}
    for batch in batches:
        for row in batch:
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
            parsed = parse_liquidation(payload, int(safe_float(row.get("event_time"))))
            if parsed is not None and parsed["notional"] >= minimum:
                merged[parsed["time"], parsed["side"], parsed["price"], parsed["quantity"]] = parsed
    return deque(sorted(merged.values(), key=lambda item: item["time"])[-2000:], maxlen=2000)
