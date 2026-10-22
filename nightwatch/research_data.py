"""Validation for completed research bars, independent of live chart ingestion."""
from __future__ import annotations

import math

from .models import Candle


def completed_history(candles, step: int, end: int, start: int = 0):
    """Reject malformed, unaligned and still-open bars before caching or analysis."""
    result = {}
    for row in candles:
        if (not math.isfinite(row.time) or not math.isfinite(row.open)
                or not math.isfinite(row.high) or not math.isfinite(row.low)
                or not math.isfinite(row.close) or not math.isfinite(row.volume)
                or not math.isfinite(row.quote_volume)
                or not 0 <= row.time <= min(end, 2**53) / 1000):
            continue
        opened = int(round(row.time * 1000))
        stamp = opened + step
        if (opened % step or not start < stamp <= end
                or row.open <= 0 or row.high <= 0 or row.low <= 0 or row.close <= 0
                or row.volume < 0 or row.quote_volume < 0
                or row.low > min(row.open, row.close)
                or row.high < max(row.open, row.close)):
            continue
        result[stamp] = row
    return result


def parse_completed_candles(payload, step: int, end: int):
    if not isinstance(payload, list):
        raise ValueError("Unexpected candle response")

    def decoded():
        for row in payload:
            try:
                if not isinstance(row, (list, tuple)) or len(row) < 8:
                    continue
                # Strict conversion: a malformed volume must not become zero.
                yield Candle(float(row[0]) / 1000, float(row[1]), float(row[2]),
                             float(row[3]), float(row[4]), float(row[5]), float(row[7]))
            except (ValueError, TypeError, OverflowError):
                continue

    return list(completed_history(decoded(), step, end).values())
