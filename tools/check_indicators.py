#!/usr/bin/env python3
"""Offline indicator regression checks: python tools/check_indicators.py.

Exercises numerical fixtures, streaming cache consistency and chart preparation.
No exchange connection, account data or order placement is involved.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import time
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nightwatch.chart.analysis import indicator_analysis
from nightwatch.chart.preparation import decimate_series
from nightwatch.indicators import PriceStudyCache, vwap_supports_interval
from nightwatch.models import Candle


START = datetime(2026, 10, 19, tzinfo=timezone.utc).timestamp()
STUDIES = {
    "EMA Trend": {"fast": 3, "medium": 5, "slow": 8},
    "Bollinger Bands": {"period": 5, "deviations": 2.0},
    "RSI": {"period": 5},
    "VWAP": {"anchor": "day"},
    "Supertrend": {"period": 5, "multiplier": 2.0},
    "ADX / DI": {"period": 5, "threshold": 25.0},
    "Relative Volume": {"period": 5},
    "Chaikin Money Flow": {"period": 5},
}
NEW_STUDIES = ("Supertrend", "ADX / DI", "Relative Volume", "Chaikin Money Flow")


def candle(index, close=100.0, *, high=None, low=None, volume=10.0, stamp=None):
    high = close + 1.0 if high is None else high
    low = close - 1.0 if low is None else low
    return Candle(
        START + index * 900 if stamp is None else stamp,
        close,
        high,
        low,
        close,
        volume,
        close * volume,
    )


def history(count=300):
    rng = np.random.default_rng(63)
    closes = 120.0 + 11.0 * np.sin(np.arange(count) / 8) + np.cumsum(rng.normal(0, .3, count))
    return [
        candle(
            index,
            float(close),
            high=float(close + rng.uniform(.1, 2.0)),
            low=float(close - rng.uniform(.1, 2.0)),
            volume=float(rng.uniform(0, 1000)),
        )
        for index, close in enumerate(closes)
    ]


def matrix(candles):
    return np.asarray([
        (c.time, c.open, c.high, c.low, c.close, c.volume, c.quote_volume)
        for c in candles
    ], dtype=float)


class IndicatorChecks(unittest.TestCase):
    def assert_series_equal(self, actual, expected):
        self.assertEqual(len(actual), len(expected))
        for left, right in zip(actual, expected):
            np.testing.assert_allclose(left, right, rtol=1e-10, atol=1e-9, equal_nan=True)

    def values(self, candles, name, settings):
        return PriceStudyCache().values(candles, name, settings)

    def test_relative_volume_excludes_live_candle_from_baseline(self):
        source = [candle(i, volume=v) for i, v in enumerate((2, 4, 6, 12, 9))]
        settings = {"period": 3}
        values, = self.values(source, "Relative Volume", settings)
        self.assertTrue(np.isnan(values[:3]).all())
        np.testing.assert_allclose(values[3:], (3.0, 27.0 / 22.0))
        source[-1] = replace(source[-1], volume=90.0)
        values, = self.values(source, "Relative Volume", settings)
        self.assertAlmostEqual(values[-1], 270.0 / 22.0)

    def test_chaikin_money_flow_hand_calculation_and_flat_range(self):
        source = [
            candle(0, 10, high=10, low=0, volume=2),
            candle(1, 0, high=10, low=0, volume=4),
            candle(2, 7.5, high=10, low=0, volume=6),
            candle(3, 5, high=5, low=5, volume=3),
        ]
        values, = self.values(source, "Chaikin Money Flow", {"period": 3})
        self.assertTrue(np.isnan(values[:2]).all())
        np.testing.assert_allclose(values[2:], (1.0 / 12.0, -1.0 / 13.0))

    def test_adx_direction_and_wilder_warmup(self):
        for direction in (1, -1):
            with self.subTest(direction=direction):
                source = [candle(i, 10 + direction * i) for i in range(8)]
                adx, plus, minus = self.values(source, "ADX / DI", {"period": 2})
                self.assertTrue(np.isnan(plus[:2]).all())
                self.assertTrue(np.isnan(minus[:2]).all())
                self.assertTrue(np.isnan(adx[:3]).all())
                np.testing.assert_allclose(adx[3:], 100.0)
                np.testing.assert_allclose(plus[2:], 50.0 if direction > 0 else 0.0)
                np.testing.assert_allclose(minus[2:], 50.0 if direction < 0 else 0.0)

    def test_equal_outside_bar_moves_do_not_claim_direction(self):
        source = [candle(i, 10, high=11 + i, low=9 - i) for i in range(8)]
        adx, plus, minus = self.values(source, "ADX / DI", {"period": 2})
        np.testing.assert_allclose(plus[2:], 0)
        np.testing.assert_allclose(minus[2:], 0)
        np.testing.assert_allclose(adx[3:], 0)

    def test_supertrend_hand_calculation_and_reversal_gaps(self):
        source = [candle(i, close) for i, close in enumerate((10, 10, 14, 15, 9, 8))]
        up, down = self.values(source, "Supertrend", {"period": 2, "multiplier": 1.0})
        np.testing.assert_allclose(up, (np.nan, np.nan, 10.5, 12.25, np.nan, np.nan), equal_nan=True)
        np.testing.assert_allclose(
            down, (np.nan, 12, np.nan, np.nan, 13.875, 11.4375), equal_nan=True,
        )
        self.assertFalse((np.isfinite(up) & np.isfinite(down)).any())

    def test_zero_volume_and_flat_prices_have_no_infinite_values(self):
        source = [candle(i, high=100, low=100, volume=0) for i in range(40)]
        for name in NEW_STUDIES:
            with self.subTest(study=name):
                values = self.values(source, name, STUDIES[name])
                self.assertFalse(any(np.isinf(series).any() for series in values))
                if name in {"Relative Volume", "Chaikin Money Flow"}:
                    self.assertTrue(np.isnan(values[0]).all())
                elif name == "ADX / DI":
                    for series in values:
                        np.testing.assert_allclose(series[np.isfinite(series)], 0)

    def test_volume_windows_recover_after_large_volume_leaves(self):
        source = [
            candle(i, 101 if i == 0 else 99, high=101, low=99, volume=1e18 if i == 0 else 1)
            for i in range(10)
        ]
        relative, = self.values(source, "Relative Volume", {"period": 3})
        flow, = self.values(source, "Chaikin Money Flow", {"period": 3})
        np.testing.assert_allclose(relative[4:], 1.0)
        np.testing.assert_allclose(flow[3:], -1.0)

    def test_future_candles_do_not_rewrite_closed_values(self):
        source = history()
        for name in NEW_STUDIES:
            complete = self.values(source, name, STUDIES[name])
            for count in (30, 87, 150):
                with self.subTest(study=name, count=count):
                    prefix = self.values(source[:count], name, STUDIES[name])
                    self.assert_series_equal(prefix, tuple(series[:count] for series in complete))

    def test_repeated_live_updates_equal_cold_calculation(self):
        original = history(150)
        for name, settings in STUDIES.items():
            with self.subTest(study=name):
                source = list(original)
                cache = PriceStudyCache()
                cache.values(source, name, settings)
                for close, volume in ((150.0, 1800.0), (80.0, 0.0), (110.0, 1200.0)):
                    source[-1] = replace(
                        original[-1], close=close, high=max(original[-1].high, close),
                        low=min(original[-1].low, close), volume=volume, quote_volume=close * volume,
                    )
                    self.assert_series_equal(
                        cache.values(source, name, settings), self.values(source, name, settings),
                    )

    def test_appends_cross_warmups_and_finalize_previous_live_bar(self):
        complete = history(65)
        for name, settings in STUDIES.items():
            with self.subTest(study=name):
                cache, source = PriceStudyCache(), []
                for final in complete:
                    if source:
                        source[-1] = complete[len(source) - 1]
                    source.append(replace(final, volume=final.volume * .5, quote_volume=final.quote_volume * .5))
                    self.assert_series_equal(
                        cache.values(source, name, settings), self.values(source, name, settings),
                    )

    def test_rolling_cache_retains_recursive_history(self):
        complete = history(120)
        for name, settings in STUDIES.items():
            with self.subTest(study=name):
                source = list(complete[:80])
                cache = PriceStudyCache()
                cache.values(source, name, settings)
                for index in range(80, len(complete)):
                    del source[0]
                    source[-1] = replace(source[-1])
                    source.append(complete[index])
                    expected = self.values(complete[:index + 1], name, settings)
                    self.assert_series_equal(
                        cache.values(source, name, settings), tuple(values[-80:] for values in expected),
                    )

    def test_backfill_and_parameter_changes_invalidate_cache(self):
        complete = history(150)
        for name, settings in STUDIES.items():
            with self.subTest(study=name):
                source, cache = list(complete[35:]), PriceStudyCache()
                cache.values(source, name, settings)
                source[:] = complete
                self.assert_series_equal(
                    cache.values(source, name, settings), self.values(source, name, settings),
                )
                changed = dict(settings)
                changed[next(iter(changed))] = "week" if name == "VWAP" else 7
                self.assert_series_equal(
                    cache.values(source, name, changed), self.values(source, name, changed),
                )

    def test_live_updates_do_not_scan_full_history(self):
        class GuardedList(list):
            guard = False

            def __iter__(self):
                if self.guard:
                    raise AssertionError("Live update iterated the full candle history")
                return super().__iter__()

            def __getitem__(self, key):
                if self.guard and isinstance(key, slice):
                    if len(range(*key.indices(len(self)))) > 30:
                        raise AssertionError("Live update sliced the full candle history")
                return super().__getitem__(key)

        for name in NEW_STUDIES:
            with self.subTest(study=name):
                source, cache = GuardedList(history(2000)), PriceStudyCache()
                cache.values(source, name, STUDIES[name])
                source[-1] = replace(source[-1], volume=50)
                source.guard = True
                cache.values(source, name, STUDIES[name])

    def test_vwap_utc_reset_and_quote_volume_weighting(self):
        source = [
            candle(0, 10, volume=2),
            candle(1, 20, volume=1),
            candle(96, 30, volume=4),
            candle(97, 50, volume=1),
        ]
        values, = self.values(source, "VWAP", {"anchor": "day"})
        np.testing.assert_allclose(values, (10, 40 / 3, 30, 34))
        # A new UTC period must not inherit the previous period's large sums.
        source[0] = replace(source[0], volume=1e18, quote_volume=1e19)
        values, = self.values(source, "VWAP", {"anchor": "day"})
        np.testing.assert_allclose(values[-2:], (30, 34))

    def test_vwap_incomplete_anchor_stays_blank_until_boundary(self):
        source = [candle(1), candle(2), candle(96, 120), candle(97, 130)]
        values, = self.values(source, "VWAP", {"anchor": "day"})
        self.assertTrue(np.isnan(values[:2]).all())
        np.testing.assert_allclose(values[2:], (120, 125))

    def test_vwap_week_and_month_boundaries(self):
        for anchor, first, second in (
            ("week", datetime(2026, 10, 19, tzinfo=timezone.utc), datetime(2026, 10, 26, tzinfo=timezone.utc)),
            ("month", datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 11, 1, tzinfo=timezone.utc)),
        ):
            with self.subTest(anchor=anchor):
                source = [
                    candle(0, 10, stamp=first.timestamp()),
                    candle(1, 20, stamp=first.timestamp() + 86400),
                    candle(2, 30, stamp=second.timestamp()),
                ]
                values, = self.values(source, "VWAP", {"anchor": anchor})
                np.testing.assert_allclose(values, (10, 15, 30))

    @unittest.skipUnless(hasattr(time, "tzset"), "tzset is unavailable on this platform")
    def test_vwap_does_not_depend_on_host_timezone(self):
        source = [candle(0, 10), candle(1, 20), candle(96, 30)]
        original = os.environ.get("TZ")
        try:
            for zone in ("UTC", "America/New_York", "Asia/Kolkata", "Europe/Warsaw"):
                with self.subTest(timezone=zone):
                    os.environ["TZ"] = zone
                    time.tzset()
                    values, = self.values(source, "VWAP", {"anchor": "day"})
                    np.testing.assert_allclose(values, (10, 15, 30))
        finally:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            time.tzset()

    def test_vwap_only_accepts_bars_within_anchor_period(self):
        for interval in ("1m", "15m", "4h", "1d"):
            for anchor in ("day", "week", "month"):
                self.assertTrue(vwap_supports_interval(interval, anchor))
        self.assertTrue(vwap_supports_interval("1w", "week"))
        self.assertTrue(vwap_supports_interval("1M", "month"))
        for interval, anchor in (("1w", "day"), ("1w", "month"), ("1M", "week"), ("1M", "day"), ("bad", "day"), ("1m", "bad")):
            self.assertFalse(vwap_supports_interval(interval, anchor))

    def test_log_chart_preserves_oscillator_units_and_relative_volume_scale(self):
        source = history()
        names = ("RSI", "ADX / DI", "Relative Volume", "Chaikin Money Flow")
        requests = tuple((name, STUDIES[name]) for name in names)
        prepared = indicator_analysis(
            self.id(), (1, 0), ("full", matrix(source)), requests, 0, len(source), 800, True,
        )
        for name in names:
            with self.subTest(study=name):
                actual = tuple(series[1] for series in prepared[name]["curves"])
                self.assert_series_equal(actual, self.values(source, name, STUDIES[name]))
        relative = prepared["Relative Volume"]
        self.assertIsNotNone(relative["scale_data"])
        self.assertIsNotNone(relative["scale_index"])
        low, high = relative["scale_index"].query(20, 150)
        raw, = self.values(source, "Relative Volume", STUDIES["Relative Volume"])
        self.assertAlmostEqual(low, np.nanmin(raw[20:150]))
        self.assertAlmostEqual(high, np.nanmax(raw[20:150]))

    def test_decimation_preserves_supertrend_gaps(self):
        source = history(6000)
        up, down = self.values(source, "Supertrend", {"period": 5, "multiplier": .5})
        times = np.asarray([c.time for c in source])
        for values in (up, down):
            selected_times, selected_values, decimated = decimate_series(times, values, 320)
            self.assertTrue(decimated)
            self.assertLess(len(selected_values), len(values))
            indices = np.searchsorted(times, selected_times)
            for left, right, first, last in zip(indices[:-1], indices[1:], selected_values[:-1], selected_values[1:]):
                if np.isfinite(first) and np.isfinite(last):
                    self.assertTrue(np.isfinite(values[left:right + 1]).all(), "Decimation bridged a trend reversal")


    def test_analysis_snapshot_omits_retired_session_download(self):
        import asyncio
        from unittest.mock import patch
        from nightwatch.networking import binance

        requested = []

        class OfflineRest:
            market_data_api_key = ""

            async def _klines_shared(self, params, priority):
                requested.append(dict(params))
                return []

            async def _request_async(self, *args, **kwargs):
                return []

        current = [candle(0)]
        with patch.object(binance, "run_async", asyncio.run):
            result = binance.BinanceRest.analysis_snapshot(OfflineRest(), "BTCUSDT", "15m", current)
        self.assertNotIn("session", result)
        self.assertEqual(result["multi"]["15m"], current)
        self.assertEqual({row["interval"] for row in requested}, {"1m", "4h", "1d"})
        self.assertTrue(all("startTime" not in row for row in requested),
                        "Retired session profile still downloads midnight-to-now candles")

    def test_analysis_delta_transport_matches_full_source(self):
        complete = history(105)
        source = list(complete[:100])
        requests = tuple(STUDIES.items())
        token = self.id()
        indicator_analysis(token, (7, 0), ("full", matrix(source)), requests, 0, len(source), 800, False)
        source[-1] = replace(source[-1], close=130, high=131, volume=1500, quote_volume=195000)
        result = indicator_analysis(token, (7, 1), ("delta", len(source), source[0].time, matrix(source[-1:])), requests, 0, len(source), 800, False)
        for name in NEW_STUDIES:
            self.assert_series_equal(tuple(curve[1] for curve in result[name]["curves"]), self.values(source, name, STUDIES[name]))
        source[-1] = complete[99]
        source.append(complete[100])
        result = indicator_analysis(token, (7, 2), ("delta", len(source), source[0].time, matrix(source[-2:])), requests, 0, len(source), 800, False)
        for name in NEW_STUDIES:
            self.assert_series_equal(tuple(curve[1] for curve in result[name]["curves"]), self.values(source, name, STUDIES[name]))
        del source[0]
        source.append(complete[101])
        result = indicator_analysis(token, (7, 3), ("delta", len(source), source[0].time, matrix(source[-2:])), requests, 0, len(source), 800, False)
        for name in NEW_STUDIES:
            expected = self.values(complete[:102], name, STUDIES[name])
            self.assert_series_equal(tuple(curve[1] for curve in result[name]["curves"]), tuple(series[1:] for series in expected))


if __name__ == "__main__":
    unittest.main(verbosity=2)
