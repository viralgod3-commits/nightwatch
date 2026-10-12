"""Validation for completed research bars, independent of live chart ingestion."""
from __future__ import annotations

import math

from .models import Candle


def completed_history(candles, step: int, end: int, start: int = 0):
    """Reject malformed, unaligned and still-open bars before caching or analysis."""
    result = {}
    for row in candles:
        values = (row.time, row.open, row.high, row.low, row.close, row.volume, row.quote_volume)
        if (not all(math.isfinite(value) for value in values)
                or not 0 <= row.time <= min(end, 2**53) / 1000):
            continue
        opened = int(round(row.time * 1000))
        stamp = opened + step
        if (opened % step or not start < stamp <= end
                or min(values[1:5]) <= 0 or min(values[5:]) < 0
                or row.low > min(row.open, row.close)
                or row.high < max(row.open, row.close)):
            continue
        result[stamp] = row
    return result


def parse_completed_candles(payload, step: int, end: int):
    if not isinstance(payload, list):
        raise ValueError("Unexpected candle response")
    candles = []
    for row in payload:
        try:
            if not isinstance(row, (list, tuple)) or len(row) < 8:
                continue
            # Strict conversion: a malformed volume must not become zero.
            candles.append(Candle(float(row[0]) / 1000, *(float(row[i]) for i in (1, 2, 3, 4, 5, 7))))
        except (ValueError, TypeError, OverflowError):
            continue
    return list(completed_history(candles, step, end).values())
