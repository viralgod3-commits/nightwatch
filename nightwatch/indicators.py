"""Vectorized chart studies, profiles, and major-level analysis."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

from .constants import INTERVAL_SECONDS
from .models import Candle, Zone


def candle_arrays(candles: list[Candle]) -> tuple[np.ndarray, ...]:
    if not candles:
        empty = np.array([], dtype=float)
        return empty, empty, empty, empty, empty, empty, empty
    return (
        np.fromiter((c.time for c in candles), dtype=float),
        np.fromiter((c.open for c in candles), dtype=float),
        np.fromiter((c.high for c in candles), dtype=float),
        np.fromiter((c.low for c in candles), dtype=float),
        np.fromiter((c.close for c in candles), dtype=float),
        np.fromiter((c.volume for c in candles), dtype=float),
        np.fromiter((c.quote_volume for c in candles), dtype=float),
    )


def atr_values(candles: list[Candle], period: int = 14) -> np.ndarray:
    period = max(1, int(period))
    _, _, high, low, close, _, _ = candle_arrays(candles)
    if len(close) == 0:
        return np.array([], dtype=float)
    previous = np.r_[close[0], close[:-1]]
    true_range = np.maximum(high - low, np.maximum(np.abs(high - previous), np.abs(low - previous)))
    output = np.full(len(true_range), np.nan)
    if len(true_range) < period:
        return output
    output[period - 1] = np.mean(true_range[:period])
    for index in range(period, len(true_range)):
        output[index] = (output[index - 1] * (period - 1) + true_range[index]) / period
    return output


@dataclass(slots=True)
class AutoFibCandidate:
    """One defensible automatic Fibonacci anchor interpretation."""

    timeframe: str
    start_time: float
    end_time: float
    start_price: float
    end_price: float
    score: float
    label: str = "ALTERNATE SWING"

    @property
    def direction(self) -> int:
        return 1 if self.end_price >= self.start_price else -1


AUTO_FIB_RATIOS: tuple[float, ...] = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)


def detect_auto_fibonacci_candidates(
    frames: dict[str, list[Candle]],
    current_interval: str,
    current_candles: list[Candle],
    current_price: float,
    zones: list[Zone] | None = None,
    maximum: int = 5,
) -> list[AutoFibCandidate]:
    """Rank active bullish impulses whose 0/1 anchors explain current price action.

    The impulse high must not have been materially superseded, the swing must not
    have broken below its origin, and at least one retracement level must overlap
    the recent trading range.  Only then do historical reactions and higher-timeframe
    confluence improve ranking.  This keeps old, attractive-looking swings from
    outranking the structure the trader is currently looking at.
    """

    maximum = max(1, min(10, int(maximum)))
    if current_price <= 0 or len(current_candles) < 35:
        return []

    sources = {str(name): list(candles) for name, candles in frames.items() if candles}
    sources[current_interval] = list(current_candles)

    timeframe_bonus = {
        "1m": 0.0,
        "5m": 1.0,
        "15m": 2.0,
        "1h": 3.0,
        "4h": 4.0,
        "1d": 5.0,
        "1w": 6.0,
    }
    timeframe_rank = {
        "1m": 1,
        "5m": 2,
        "15m": 3,
        "1h": 4,
        "4h": 5,
        "1d": 6,
        "1w": 7,
    }
    all_candidates: list[tuple[AutoFibCandidate, int, float]] = []
    zone_centers = [
        (float(zone.low) + float(zone.high)) * 0.5
        for zone in (zones or [])
        if zone.low > 0 and zone.high > 0
    ]

    for timeframe, raw_candles in sources.items():
        candles = raw_candles[-min(760, len(raw_candles)) :]
        count = len(candles)
        if count < 35:
            continue

        atr = atr_values(candles, 14)
        finite_atr = atr[np.isfinite(atr) & (atr > 0)]
        fallback_atr = float(np.median(finite_atr[-120:])) if len(finite_atr) else 0.0
        if fallback_atr <= 0:
            fallback_atr = max(
                current_price * 0.003,
                (max(c.high for c in candles) - min(c.low for c in candles)) / 80.0,
                1e-12,
            )
        closes = np.fromiter((candle.close for candle in candles), dtype=float)
        trend_window = min(180, count)
        trend_slice = closes[-trend_window:]
        third = max(8, len(trend_slice) // 3)
        earlier_price = float(np.median(trend_slice[:third]))
        recent_price = float(np.median(trend_slice[-third:]))
        structural_gain = recent_price / max(earlier_price, 1e-12) - 1.0
        if structural_gain <= 0 and current_price <= earlier_price:
            continue
        volatility_fraction = fallback_atr / max(current_price, 1e-12)
        bull_trend_strength = min(
            4.0,
            max(0.0, structural_gain) / max(volatility_fraction * 3.0, 0.004),
        )
        volumes = np.fromiter((max(0.0, candle.volume) for candle in candles), dtype=float)
        span = 3 if count < 180 else 4
        start = max(16, count - 620)
        stop = count - span
        pivots: list[tuple[int, str, float, float]] = []

        for index in range(start, stop):
            atr_here = float(atr[index]) if np.isfinite(atr[index]) and atr[index] > 0 else fallback_atr
            if atr_here <= 0:
                continue
            candle = candles[index]
            local = candles[index - span : index + span + 1]
            left = candles[index - span : index]
            right = candles[index + 1 : index + span + 1]
            age = count - 1 - index
            recency = 1.0 / (1.0 + age / 115.0)
            rolling_start = max(0, index - 24)
            average_volume = float(np.mean(volumes[rolling_start : index + 1])) if index >= rolling_start else 0.0
            relative_volume = float(volumes[index] / max(average_volume, 1e-12)) if average_volume > 0 else 1.0
            candle_range = max(candle.high - candle.low, 1e-12)

            if candle.low <= min(item.low for item in local):
                left_peak = max((item.high for item in left), default=candle.high)
                right_peak = max((item.high for item in right), default=candle.high)
                prominence = min(left_peak - candle.low, right_peak - candle.low) / atr_here
                wick = max(0.0, min(candle.open, candle.close) - candle.low) / candle_range
                score = (
                    min(max(prominence, 0.0), 5.5) * 7.0
                    + min(relative_volume, 3.0) * 1.8
                    + min(wick, 1.0) * 5.0
                    + recency * 10.0
                    + timeframe_bonus.get(timeframe, 2.0)
                )
                if prominence >= 0.55:
                    pivots.append((index, "low", float(candle.low), score))

            if candle.high >= max(item.high for item in local):
                left_base = min((item.low for item in left), default=candle.low)
                right_base = min((item.low for item in right), default=candle.low)
                prominence = min(candle.high - left_base, candle.high - right_base) / atr_here
                wick = max(0.0, candle.high - max(candle.open, candle.close)) / candle_range
                score = (
                    min(max(prominence, 0.0), 5.5) * 7.0
                    + min(relative_volume, 3.0) * 1.8
                    + min(wick, 1.0) * 5.0
                    + recency * 10.0
                    + timeframe_bonus.get(timeframe, 2.0)
                )
                if prominence >= 0.55:
                    pivots.append((index, "high", float(candle.high), score))


        terminal_start = max(start, count - 48)
        terminal_index = max(
            range(terminal_start, count),
            key=lambda value: candles[value].high,
        )
        if terminal_index >= stop:
            atr_here = (
                float(atr[terminal_index])
                if np.isfinite(atr[terminal_index]) and atr[terminal_index] > 0
                else fallback_atr
            )
            base_start = max(start, terminal_index - 32)
            preceding_low = min(
                item.low for item in candles[base_start : terminal_index + 1]
            )
            prominence = (
                candles[terminal_index].high - preceding_low
            ) / max(atr_here, 1e-12)
            if prominence >= 1.20:
                pivots.append(
                    (
                        terminal_index,
                        "high",
                        float(candles[terminal_index].high),
                        min(prominence, 6.0) * 7.0
                        + 13.0
                        + timeframe_bonus.get(timeframe, 2.0),
                    )
                )

        if len(pivots) < 2:
            continue


        reduced: list[tuple[int, str, float, float]] = []
        for kind in ("low", "high"):
            strongest = sorted(
                (pivot for pivot in pivots if pivot[1] == kind),
                key=lambda item: item[3],
                reverse=True,
            )[:18]
            reduced.extend(strongest)
        pivots = sorted(reduced, key=lambda item: item[0])

        for left_index, first in enumerate(pivots[:-1]):
            i, first_kind, first_price, first_score = first
            for second in pivots[left_index + 1 :]:
                j, second_kind, second_price, second_score = second
                if first_kind != "low" or second_kind != "high":
                    continue
                separation = j - i
                if separation < 6 or separation > 430:
                    continue

                atr_end = float(atr[j]) if np.isfinite(atr[j]) and atr[j] > 0 else fallback_atr
                price_span = abs(second_price - first_price)
                normalized_span = price_span / max(atr_end, 1e-12)
                if normalized_span < 3.1:
                    continue

                segment = candles[i : j + 1]


                if min(item.low for item in segment) < first_price - atr_end * 0.38:
                    continue
                if max(item.high for item in segment) > second_price + atr_end * 0.38:
                    continue

                segment_closes = np.fromiter(
                    (item.close for item in segment), dtype=float
                )
                travelled = float(np.sum(np.abs(np.diff(segment_closes))))
                path_efficiency = min(1.0, price_span / max(travelled, price_span))
                impulse_return = second_price / max(first_price, 1e-12) - 1.0
                if impulse_return <= 0:
                    continue

                fib_levels = [
                    second_price - price_span * ratio
                    for ratio in AUTO_FIB_RATIOS
                ]
                age = count - 1 - j
                if age > 220:
                    continue
                post_all = candles[j + 1 :]


                if post_all and max(item.high for item in post_all) > second_price + max(
                    atr_end * 0.40,
                    price_span * 0.04,
                ):
                    continue

                if post_all and min(item.low for item in post_all) < first_price - max(
                    atr_end * 0.30,
                    price_span * 0.025,
                ):
                    continue

                post = post_all[:180]
                reaction_score = 0.0
                reaction_hits = 0
                tolerance = max(atr_end * 0.17, price_span * 0.0035)
                level_weights = {
                    0.236: 0.75,
                    0.382: 1.00,
                    0.5: 1.12,
                    0.618: 1.25,
                    0.786: 0.82,
                }
                for ratio in (0.236, 0.382, 0.5, 0.618, 0.786):
                    level = second_price - price_span * ratio
                    touch_at: int | None = None
                    for post_index, later in enumerate(post):
                        if later.low - tolerance <= level <= later.high + tolerance:
                            touch_at = post_index
                            break
                    if touch_at is None:
                        continue
                    reaction_hits += 1
                    follow = post[touch_at : min(len(post), touch_at + 9)]
                    reaction = (max(item.high for item in follow) - level) / max(atr_end, 1e-12)
                    reaction_score += level_weights[ratio] * (1.5 + min(max(reaction, 0.0), 2.8) * 1.8)

                confluence = 0
                if zone_centers:
                    zone_tolerance = max(price_span * 0.008, current_price * 0.0020)
                    for level in fib_levels[1:-1]:
                        if any(abs(level - zone) <= zone_tolerance for zone in zone_centers):
                            confluence += 1

                recency = 1.0 / (1.0 + age / 42.0)
                retracement_depth = (second_price - current_price) / max(price_span, 1e-12)
                if retracement_depth < -0.25 or retracement_depth > 0.96:
                    continue
                recent = candles[-min(40, count) :]
                recent_low = min(item.low for item in recent)
                recent_high = max(item.high for item in recent)
                action_tolerance = max(atr_end * 0.35, price_span * 0.008)
                active_levels = fib_levels[:-1]
                if not any(
                    recent_low - action_tolerance <= level <= recent_high + action_tolerance
                    for level in active_levels
                ):
                    continue
                nearest_distance = min(abs(level / current_price - 1.0) for level in fib_levels if level > 0)
                usefulness = max(0.0, 1.0 - nearest_distance / 0.055)
                anchor_quality = (first_score + second_score) * 0.5

                score = (
                    anchor_quality * 0.42
                    + min(normalized_span, 14.0) * 2.7
                    + min(math.log1p(separation), 6.2) * 2.4
                    + reaction_score
                    + min(reaction_hits, 4) * 1.4
                    + min(confluence, 3) * 5.0
                    + recency * 22.0
                    + usefulness * 15.0
                    + path_efficiency * 11.0
                    + min(impulse_return / max(volatility_fraction, 1e-6), 12.0) * 1.4
                    + bull_trend_strength * 4.0
                    + timeframe_bonus.get(timeframe, 2.0)
                    + (7.0 if timeframe == current_interval else 0.0)
                )
                candidate = AutoFibCandidate(
                    timeframe=timeframe,
                    start_time=float(candles[i].time),
                    end_time=float(candles[j].time),
                    start_price=float(first_price),
                    end_price=float(second_price),
                    score=float(score),
                )
                all_candidates.append((candidate, age, float(separation)))

    if not all_candidates:
        return []

    all_candidates.sort(key=lambda item: item[0].score, reverse=True)
    selected: list[tuple[AutoFibCandidate, int, float]] = [all_candidates[0]]
    primary = all_candidates[0][0]
    per_timeframe: dict[str, int] = {primary.timeframe: 1}

    def duplicates_selected(candidate: AutoFibCandidate) -> bool:
        price_span = abs(candidate.end_price - candidate.start_price)
        for existing, _old_age, _old_sep in selected:
            time_span = max(
                abs(existing.end_time - existing.start_time),
                abs(candidate.end_time - candidate.start_time),
                1.0,
            )
            same_start = abs(candidate.start_time - existing.start_time) <= time_span * 0.10
            same_end = abs(candidate.end_time - existing.end_time) <= time_span * 0.10
            price_scale = max(
                abs(existing.end_price - existing.start_price),
                price_span,
                current_price * 0.002,
            )
            same_prices = (
                abs(candidate.start_price - existing.start_price) <= price_scale * 0.08
                and abs(candidate.end_price - existing.end_price) <= price_scale * 0.08
            )
            if (same_start and same_end) or same_prices:
                return True
        return False


    primary_time_span = max(primary.end_time - primary.start_time, 1.0)
    primary_price_span = max(primary.end_price - primary.start_price, current_price * 0.002)
    variants: list[tuple[AutoFibCandidate, int, float]] = []
    for item in all_candidates[1:]:
        candidate = item[0]
        if candidate.timeframe != primary.timeframe:
            continue
        same_impulse_high = (
            abs(candidate.end_time - primary.end_time) <= primary_time_span * 0.18
            and abs(candidate.end_price - primary.end_price) <= primary_price_span * 0.06
        )
        distinct_swing_low = (
            abs(candidate.start_time - primary.start_time) > primary_time_span * 0.12
            or abs(candidate.start_price - primary.start_price) > primary_price_span * 0.08
        )
        if same_impulse_high and distinct_swing_low and not duplicates_selected(candidate):
            variants.append(item)
    variant_count = 0
    for item in variants:
        if variant_count >= 2 or len(selected) >= maximum:
            break
        if duplicates_selected(item[0]):
            continue
        selected.append(item)
        candidate = item[0]
        per_timeframe[candidate.timeframe] = per_timeframe.get(candidate.timeframe, 0) + 1
        variant_count += 1

    for item in all_candidates:
        if len(selected) >= maximum:
            break
        candidate = item[0]
        if per_timeframe.get(candidate.timeframe, 0) >= 3:
            continue
        if duplicates_selected(candidate):
            continue
        selected.append(item)
        per_timeframe[candidate.timeframe] = per_timeframe.get(candidate.timeframe, 0) + 1

    current_rank = timeframe_rank.get(current_interval, 0)
    for index, (candidate, age, separation) in enumerate(selected):
        if index == 0:
            label = "PRIMARY BULL SWING"
        elif (
            candidate.timeframe == primary.timeframe
            and abs(candidate.end_time - primary.end_time) <= primary_time_span * 0.18
            and abs(candidate.end_price - primary.end_price) <= primary_price_span * 0.06
        ):
            label = (
                "PARENT SWING LOW"
                if candidate.start_time < primary.start_time
                or candidate.start_price < primary.start_price
                else "HIGHER SWING LOW"
            )
        elif timeframe_rank.get(candidate.timeframe, 0) > current_rank:
            label = "HIGHER-TF BULL SWING"
        elif age <= 32:
            label = "RECENT BULL SWING"
        elif separation >= 120:
            label = "PARENT BULL SWING"
        else:
            label = "PRIOR BULL SWING"
        candidate.label = label

    return [candidate for candidate, _age, _separation in selected]


def bollinger_values(candles: list[Candle], period: int = 20, deviations: float = 2.0) -> tuple[np.ndarray, ...]:
    period = max(1, int(period))
    close = np.fromiter((c.close for c in candles), dtype=float, count=len(candles))
    middle = np.full(len(close), np.nan)
    upper = np.full(len(close), np.nan)
    lower = np.full(len(close), np.nan)
    if len(close) < period:
        return middle, upper, lower
    windows = np.lib.stride_tricks.sliding_window_view(close, period)
    # NumPy's std materializes window deviations. Bound that temporary to ~2 MiB
    # even at maximum chart history, retaining the stable two-pass calculation.
    chunk_size = max(1, 262_144 // period)
    for start in range(0, len(windows), chunk_size):
        chunk = windows[start:start + chunk_size]
        means = chunk.mean(axis=1)
        width = chunk.std(axis=1) * deviations
        target = slice(start + period - 1, start + period - 1 + len(chunk))
        middle[target] = means
        upper[target] = means + width
        lower[target] = means - width
    return middle, upper, lower


def volume_profile(candles: list[Candle], bins: int = 48) -> tuple[np.ndarray, np.ndarray]:
    """OHLCV estimate: distribute each candle's volume across its price range.

    One algorithm at every history size; chunking affects memory, not results.
    This is an estimate, not exchange trade-by-price volume.
    """
    if not candles:
        return np.array([], dtype=float), np.array([], dtype=float)
    bins = max(1, int(bins))
    _, _, high, low, _, volumes, _ = candle_arrays(candles)
    valid = np.isfinite(high) & np.isfinite(low) & np.isfinite(volumes) & (high >= low) & (volumes > 0)
    high, low, volumes = high[valid], low[valid], volumes[valid]
    if not len(volumes):
        return np.array([], dtype=float), np.array([], dtype=float)
    minimum, maximum = float(low.min()), float(high.max())
    if maximum <= minimum:
        return np.array([minimum]), np.array([volumes.sum()])
    edges = np.linspace(minimum, maximum, bins + 1)
    profile = np.zeros(bins, dtype=float)
    for start in range(0, len(volumes), 1024):
        hi, lo, volume = high[start:start+1024], low[start:start+1024], volumes[start:start+1024]
        overlap = np.maximum(0.0, np.minimum(hi[:, None], edges[1:]) - np.maximum(lo[:, None], edges[:-1]))
        spans = overlap.sum(axis=1)
        weights = np.divide(overlap, spans[:, None], out=np.zeros_like(overlap), where=spans[:, None] > 0)
        profile += (weights * volume[:, None]).sum(axis=0)
        flat = spans <= 0
        indices = np.clip(np.searchsorted(edges, lo[flat], side="right") - 1, 0, bins - 1)
        np.add.at(profile, indices, volume[flat])
    return (edges[:-1] + edges[1:]) * 0.5, profile


def profile_levels(centers: np.ndarray, volumes: np.ndarray, count: int = 4) -> list[float]:
    if len(centers) == 0 or not np.any(volumes > 0):
        return []
    order = np.argsort(volumes)[::-1]
    selected: list[float] = []
    minimum_gap = (centers.max() - centers.min()) / max(12, len(centers))
    for index in order:
        level = float(centers[index])
        if all(abs(level - old) >= minimum_gap for old in selected):
            selected.append(level)
        if len(selected) >= count:
            break
    return selected


@dataclass(frozen=True)
class MajorLevelCandidate:
    timeframe: str
    origin: str
    level: float
    zone_low: float
    zone_high: float
    reaction: float
    displacement: float
    wick_ratio: float
    touches: int
    held_support: int
    held_resistance: int
    relative_volume: float
    profile_confirmed: bool
    recency: float


def build_major_level_candidates(
    candles: list[Candle],
    timeframe: str,
) -> list[MajorLevelCandidate]:
    """Build price-independent level evidence for one timeframe."""
    if len(candles) < 70:
        return []
    pivot_span = {"15m": 4, "4h": 3, "1d": 2}
    zone_scale = {"15m": 0.17, "4h": 0.14, "1d": 0.12}
    atr = atr_values(candles)
    volumes = np.fromiter((candle.volume for candle in candles), dtype=float)
    rolling_volume = rolling_mean_series(volumes, 30)
    profile_source = candles[-min(360, len(candles)) :]
    profile_centers, profile_volume = volume_profile(profile_source, bins=56)
    volume_nodes = profile_levels(profile_centers, profile_volume, count=5)
    span = pivot_span.get(timeframe, 3)
    start = max(30, len(candles) - 560)
    stop = len(candles) - span
    candidates: list[MajorLevelCandidate] = []

    for index in range(start, stop):
        if not np.isfinite(atr[index]) or atr[index] <= 0:
            continue
        candle = candles[index]
        local = candles[index - span : index + span + 1]
        pivot_types: list[tuple[str, float]] = []
        if candle.low <= min(item.low for item in local):
            pivot_types.append(("low", candle.low))
        if candle.high >= max(item.high for item in local):
            pivot_types.append(("high", candle.high))
        if not pivot_types:
            continue

        for origin, level in pivot_types:
            if level <= 0:
                continue
            half_width = max(
                atr[index] * zone_scale.get(timeframe, 0.15),
                level * 0.00022,
            )
            zone_low = max(1e-12, level - half_width)
            zone_high = level + half_width
            future = candles[index + 1 : min(len(candles), index + 19)]
            if len(future) < span:
                continue
            if origin == "low":
                reaction = (max(item.high for item in future) - level) / atr[index]
                displacement = (max(item.close for item in future) - level) / atr[index]
                wick = max(0.0, min(candle.open, candle.close) - candle.low)
            else:
                reaction = (level - min(item.low for item in future)) / atr[index]
                displacement = (level - min(item.close for item in future)) / atr[index]
                wick = max(0.0, candle.high - max(candle.open, candle.close))
            if reaction < 1.35 or displacement < 0.70:
                continue

            candle_range = max(candle.high - candle.low, 1e-12)
            wick_ratio = min(1.0, wick / candle_range)
            touches = 0
            held_support = 0
            held_resistance = 0
            last_touch = -100
            for later_index in range(index + span + 1, len(candles)):
                later = candles[later_index]
                if (
                    later_index - last_touch >= max(3, span)
                    and later.low <= zone_high
                    and later.high >= zone_low
                ):
                    touches += 1
                    last_touch = later_index
                    if later.close >= zone_low:
                        held_support += 1
                    if later.close <= zone_high:
                        held_resistance += 1
            relative_volume = volumes[index] / max(rolling_volume[index], 1e-12)
            profile_tolerance = max(atr[index] * 0.38, level * 0.0014)
            profile_confirmed = any(
                abs(level - node) <= profile_tolerance for node in volume_nodes
            )
            age = len(candles) - 1 - index
            recency = 9.0 / (1.0 + age / 110.0)
            candidates.append(
                MajorLevelCandidate(
                    timeframe=timeframe,
                    origin=origin,
                    level=float(level),
                    zone_low=float(zone_low),
                    zone_high=float(zone_high),
                    reaction=float(reaction),
                    displacement=float(displacement),
                    wick_ratio=float(wick_ratio),
                    touches=int(touches),
                    held_support=int(held_support),
                    held_resistance=int(held_resistance),
                    relative_volume=float(relative_volume),
                    profile_confirmed=bool(profile_confirmed),
                    recency=float(recency),
                )
            )
    return candidates


def rank_major_level_candidates(
    frame_candidates: dict[str, list[MajorLevelCandidate]],
    current_price: float,
    minimum_score: float = 48.0,
    maximum_levels: int = 4,
) -> list[Zone]:
    if current_price <= 0:
        return []
    timeframe_bonus = {"15m": 0.0, "4h": 11.0, "1d": 19.0}
    timeframe_rank = {"15m": 1, "4h": 2, "1d": 3}
    distance_limit = {"15m": 0.10, "4h": 0.23, "1d": 0.40}
    zones: list[Zone] = []
    threshold = max(1.0, float(minimum_score))

    for timeframe, candidates in frame_candidates.items():
        for candidate in candidates:
            level = candidate.level
            distance = abs(level / current_price - 1.0)
            if distance > distance_limit.get(timeframe, 0.16):
                continue
            kind = "support" if level <= current_price else "resistance"
            role_flip = (candidate.origin == "high" and kind == "support") or (
                candidate.origin == "low" and kind == "resistance"
            )
            held = (
                candidate.held_support
                if kind == "support"
                else candidate.held_resistance
            )
            hold_ratio = held / max(candidate.touches, 1)
            price_action_score = (
                min(candidate.reaction, 4.5) * 11.0
                + min(candidate.displacement, 3.5) * 6.0
                + candidate.wick_ratio * 10.0
                + min(candidate.touches, 3) * 4.0
                + hold_ratio * 9.0
                + candidate.recency
                - max(0, candidate.touches - 3) * 5.5
            )
            supporting_score = (
                min(candidate.relative_volume, 3.0) * 3.0
                + (9.0 if candidate.profile_confirmed else 0.0)
                + timeframe_bonus.get(timeframe, 4.0)
                + (6.0 if role_flip else 0.0)
                - min(18.0, distance * 70.0)
            )
            score = price_action_score + supporting_score
            if score < threshold:
                continue
            evidence = ["PRICE ACTION"]
            if role_flip:
                evidence.append("ROLE FLIP")
            if candidate.profile_confirmed:
                evidence.append("VOLUME NODE")
            zones.append(
                Zone(
                    candidate.zone_low,
                    candidate.zone_high,
                    score,
                    timeframe,
                    candidate.touches,
                    kind,
                    " + ".join(evidence),
                )
            )

    zones.sort(key=lambda zone: zone.score, reverse=True)
    merged: list[Zone] = []
    for zone in zones:
        center = (zone.low + zone.high) * 0.5
        match = next(
            (
                existing
                for existing in merged
                if existing.kind == zone.kind
                and abs((existing.low + existing.high) * 0.5 - center)
                <= max(current_price * 0.0030, (existing.high - existing.low) * 1.5)
            ),
            None,
        )
        if match is None:
            merged.append(zone)
            continue
        different_timeframe = match.timeframe != zone.timeframe
        existing_timeframe = match.timeframe
        existing_score = match.score
        if zone.score > match.score:
            match.low, match.high = zone.low, zone.high
            match.source = zone.source
        match.score = max(existing_score, zone.score) + min(existing_score, zone.score) * 0.16
        match.touches = max(match.touches, zone.touches)
        if different_timeframe:
            match.score += 9.0
            match.source = "MULTI-TF PRICE ACTION"
            match.timeframe = max(
                (existing_timeframe, zone.timeframe),
                key=lambda value: timeframe_rank.get(value, 0),
            )
        elif zone.score > existing_score:
            match.timeframe = zone.timeframe

    maximum_levels = max(1, min(8, int(maximum_levels)))
    per_side = max(1, math.ceil(maximum_levels / 2))
    selected: list[Zone] = []
    minimum_gap = current_price * 0.0045
    for kind in ("support", "resistance"):
        side = sorted(
            (zone for zone in merged if zone.kind == kind),
            key=lambda zone: zone.score,
            reverse=True,
        )
        for zone in side:
            center = (zone.low + zone.high) * 0.5
            if all(
                abs(center - (old.low + old.high) * 0.5) >= minimum_gap
                for old in selected
            ):
                selected.append(zone)
            if sum(old.kind == kind for old in selected) >= per_side:
                break
    if len(selected) < maximum_levels:
        for zone in sorted(merged, key=lambda item: item.score, reverse=True):
            if zone in selected:
                continue
            center = (zone.low + zone.high) * 0.5
            if all(
                abs(center - (old.low + old.high) * 0.5) >= minimum_gap
                for old in selected
            ):
                selected.append(zone)
            if len(selected) >= maximum_levels:
                break
    selected.sort(key=lambda zone: zone.high, reverse=True)
    return selected[:maximum_levels]


def ema_series(values: np.ndarray, period: int) -> np.ndarray:
    """Return an EMA with an explicit warm-up instead of back-filled values."""
    output = np.full(len(values), np.nan, dtype=float)
    period = max(2, int(period))
    if len(values) < period:
        return output
    output[period - 1] = float(np.mean(values[:period]))
    weight = 2.0 / (period + 1.0)
    for index in range(period, len(values)):
        output[index] = values[index] * weight + output[index - 1] * (1.0 - weight)
    return output


def _rsi_components(values, period):
    values = np.asarray(values, dtype=float)
    period = max(2, int(period))
    output = np.full(len(values), np.nan)
    avg_gains, avg_losses = output.copy(), output.copy()
    if len(values) <= period:
        return output, avg_gains, avg_losses
    changes = np.diff(values)
    gains, losses = np.maximum(changes, 0.0), np.maximum(-changes, 0.0)
    avg_gains[period], avg_losses[period] = gains[:period].mean(), losses[:period].mean()
    for i in range(period + 1, len(values)):
        avg_gains[i] = (avg_gains[i-1] * (period-1) + gains[i-1]) / period
        avg_losses[i] = (avg_losses[i-1] * (period-1) + losses[i-1]) / period
    total = avg_gains + avg_losses
    np.divide(100.0 * avg_gains, total, out=output, where=total > 0)
    output[total == 0] = 50.0
    return output, avg_gains, avg_losses


def rolling_mean_series(values: np.ndarray, period: int) -> np.ndarray:
    output = np.full(len(values), np.nan, dtype=float)
    period = max(1, int(period))
    if len(values) < period:
        return output
    sums = np.cumsum(np.r_[0.0, values])
    output[period - 1 :] = (sums[period:] - sums[:-period]) / period
    return output


def rolling_previous_extreme(values: np.ndarray, period: int, maximum: bool) -> np.ndarray:
    """Rolling high/low that excludes the current candle."""
    output = np.full(len(values), np.nan, dtype=float)
    period = max(2, int(period))
    candidates: deque[int] = deque()
    for index, value in enumerate(values):
        while candidates and candidates[0] < index - period:
            candidates.popleft()
        if index >= period and candidates:
            output[index] = values[candidates[0]]
        while candidates and (
            value >= values[candidates[-1]] if maximum else value <= values[candidates[-1]]
        ):
            candidates.pop()
        candidates.append(index)
    return output


def vwap_supports_interval(interval: str, anchor: str) -> bool:
    """A candle must fit wholly inside its UTC VWAP anchor period."""
    if interval not in INTERVAL_SECONDS or anchor not in {"day", "week", "month"}:
        return False
    return (
        INTERVAL_SECONDS[interval] <= 86400
        or (interval, anchor) in {("1w", "week"), ("1M", "month")}
    )


def _vwap_components(candles, anchor="week"):
    times, _, high, low, close, volume, quote = candle_arrays(candles)
    if anchor == "month":
        groups = times.astype("datetime64[s]").astype("datetime64[M]").astype("datetime64[s]").astype(float)
    elif anchor == "day":
        groups = np.floor(times / 86400) * 86400
    else:
        groups = np.floor((times + 259200) / 604800) * 604800 - 259200
    if not len(times):
        return times.copy(), groups, volume, quote
    volume = np.maximum(volume, 0)
    quote = np.where(quote > 0, quote, (high + low + close) / 3.0 * volume)
    starts = np.flatnonzero(np.r_[True, groups[1:] != groups[:-1]])
    counts = np.diff(np.r_[starts, len(times)])
    cv, cq = np.cumsum(volume), np.cumsum(quote)
    cv -= np.repeat(np.r_[0.0, cv[starts[1:]-1]], counts)
    cq -= np.repeat(np.r_[0.0, cq[starts[1:]-1]], counts)
    values = np.divide(cq, cv, out=np.full(len(times), np.nan), where=cv > 0)
    if times[0] > groups[0]:
        values[groups == groups[0]] = np.nan
    return values, groups, cv, cq


def donchian_values(candles, period=20):
    _, _, high, low, _, _, _ = candle_arrays(candles)
    upper = rolling_previous_extreme(high, period, True)
    lower = rolling_previous_extreme(low, period, False)
    return upper, (upper + lower) * 0.5, lower


class PriceStudyCache:
    """Native-bar studies independent of viewport/LOD; live changes update one row.

    Historical arrays rebuild on backfill or settings changes; new bars extend them.
    Candle objects are replaced by ChartWorkspace, never mutated in place.
    """

    def __init__(self):
        self.entries = {}

    @staticmethod
    def cache_key(candles, settings, *, length=None):
        count = len(candles) if length is None else max(0, min(int(length), len(candles)))
        if count <= 0:
            return None
        return (
            id(candles),
            count,
            id(candles[0]),
            id(candles[count - 2]) if count > 1 else None,
            candles[count - 1].time,
            tuple(sorted(settings.items())),
            id(candles[1]) if count > 1 else None,
        )

    @staticmethod
    def _incremental_mode(candles, key, cached):
        if cached is None or cached[0] == key or key is None:
            return None
        old = cached[0]
        same_history = (
            key[0] == old[0]
            and key[5] == old[5]
            and len(candles) > 2
            and id(candles[-3]) == old[3]
            and candles[-2].time == old[4]
        )
        if not same_history:
            return None
        if key[1] == old[1] + 1 and key[2] == old[2]:
            return "appended"
        if key[1] == old[1] and key[2] == old[6]:
            return "rolled"
        return None


    def values(self, candles, name, settings):
        if not candles:
            return ()
        key = self.cache_key(candles, settings)
        cached = self.entries.get(name)
        if cached is not None and cached[0] != key:
            old, arrays, state = cached
            mode = self._incremental_mode(candles, key, cached)
            if mode is not None:
                if mode == "rolled":
                    arrays = tuple(a[1:] for a in arrays)
                    state = tuple(a[1:] for a in state) if state is not None else None


                self._update_tail(candles, len(candles)-2, name, settings, arrays, state)
                arrays = tuple(np.append(a, np.nan) for a in arrays)
                state = tuple(np.append(a, np.nan) for a in state) if state is not None else None
                cached = key, arrays, state
                self.entries[name] = cached
        if cached is None or cached[0] != key:
            closes = np.fromiter((c.close for c in candles), dtype=float)
            state = None
            if name == "Bollinger Bands":
                arrays = bollinger_values(candles, int(settings["period"]), float(settings["deviations"]))
            elif name == "ATR":
                arrays = (atr_values(candles, int(settings["period"])),)
            elif name == "RSI":
                rsi, gains, losses = _rsi_components(closes, int(settings["period"]))
                arrays, state = (rsi,), (gains, losses)
            elif name == "EMA Trend":
                arrays = tuple(ema_series(closes, int(settings[k])) for k in ("fast", "medium", "slow"))
            elif name == "VWAP":
                values, groups, volumes, quotes = _vwap_components(candles, settings.get("anchor", "week"))
                arrays, state = (values,), (groups, volumes, quotes)
            elif name == "Donchian Channels":
                arrays = donchian_values(candles, int(settings["period"]))
            else:
                raise ValueError(name)
            self.entries[name] = key, arrays, state
            return arrays
        _, arrays, state = cached
        self._update_tail(candles, len(candles)-1, name, settings, arrays, state)
        return arrays

    @staticmethod
    def _update_tail(candles, index, name, settings, arrays, state):
        n, last = index+1, candles[index]
        if name == "Bollinger Bands":
            period = int(settings["period"])
            if n >= period:
                window = np.fromiter((c.close for c in candles[n-period:n]), dtype=float)
                mean, width = window.mean(), window.std() * float(settings["deviations"])
                for values, value in zip(arrays, (mean, mean+width, mean-width)):
                    values[index] = value
        elif name == "EMA Trend":
            for values, k in zip(arrays, ("fast", "medium", "slow")):
                period = int(settings[k])
                if n == period:
                    values[index] = np.mean([c.close for c in candles[:n]])
                elif n > period:
                    alpha = 2.0 / (period+1)
                    values[index] = alpha*last.close + (1-alpha)*values[index-1]
        elif name == "ATR":
            period = int(settings["period"])
            if n == period:
                arrays[0][index] = atr_values(candles[:n], period)[-1]
            elif n > period:
                tr = max(last.high-last.low, abs(last.high-candles[index-1].close), abs(last.low-candles[index-1].close))
                arrays[0][index] = (arrays[0][index-1]*(period-1)+tr)/period
        elif name == "RSI":
            period = int(settings["period"])
            if n == period+1:
                rsi, gains, losses = _rsi_components(np.array([c.close for c in candles[:n]]), period)
                arrays[0][index], state[0][index], state[1][index] = rsi[-1], gains[-1], losses[-1]
            elif n > period+1:
                delta = last.close-candles[index-1].close
                gain = (state[0][index-1]*(period-1)+max(delta, 0))/period
                loss = (state[1][index-1]*(period-1)+max(-delta, 0))/period
                state[0][index], state[1][index] = gain, loss
                arrays[0][index] = 100*gain/(gain+loss) if gain+loss > 0 else 50.0
        elif name == "VWAP":
            groups, volumes, quotes = state
            anchor = settings.get("anchor", "week")
            if anchor == "month":
                group = float(np.datetime64(int(last.time), "s").astype("datetime64[M]").astype("datetime64[s]").astype(np.int64))
            elif anchor == "day":
                group = math.floor(last.time/86400)*86400
            else:
                group = math.floor((last.time+259200)/604800)*604800-259200
            groups[index] = group
            same = index > 0 and group == groups[index-1]
            volume = max(0.0, last.volume)
            quote = last.quote_volume if last.quote_volume > 0 else (last.high+last.low+last.close)/3*volume
            denominator = (volumes[index-1] if same else 0.0) + volume
            numerator = (quotes[index-1] if same else 0.0) + quote
            volumes[index], quotes[index] = denominator, numerator
            complete = group != groups[0] or candles[0].time <= groups[0]
            arrays[0][index] = numerator/denominator if denominator > 0 and complete else np.nan
        elif name == "Donchian Channels":
            period = int(settings["period"])
            if index >= period:
                previous = candles[index-period:index]
                upper, lower = max(c.high for c in previous), min(c.low for c in previous)
                arrays[0][index], arrays[1][index], arrays[2][index] = upper, (upper+lower)*0.5, lower
