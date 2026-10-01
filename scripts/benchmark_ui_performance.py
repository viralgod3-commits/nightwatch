"""Offline watchlist and magnetic-order overlay benchmarks.

NIGHTWATCH_BENCH_SOURCE selects a baseline checkout. Use the same interpreter,
Qt platform, fonts and sample count for both runs. These callback measurements
are not monitor FPS or GPU throughput. No network or trading gateway is used.
"""
from __future__ import annotations

import argparse
from collections import deque
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

SOURCE = Path(os.environ.get("NIGHTWATCH_BENCH_SOURCE", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(SOURCE))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.pop("BINANCE_API_KEY", None)
os.environ.pop("BINANCE_API_SECRET", None)

from PySide6 import QtCore, QtWidgets
from nightwatch.chart.analysis import shutdown_analysis
from nightwatch.chart.preparation import prepare_snapshot
from nightwatch.chart.workspace import ChartWorkspace
from nightwatch.constants import INTERVAL_SECONDS
from nightwatch.models import Candle
from nightwatch.theme import DEFAULT_THEME_NAME, THEMES
from nightwatch.ui import market_widgets


def measure(callback, samples):
    durations = []
    for index in range(samples):
        started = time.perf_counter()
        callback(index)
        durations.append((time.perf_counter() - started) * 1000)
    ordered = sorted(durations)
    return {"samples": samples, "median_ms": round(statistics.median(durations), 3),
            "p95_ms": round(ordered[max(0, math.ceil(samples * .95) - 1)], 3),
            "max_ms": round(max(durations), 3)}


def chart_fixture(app, orders=20):
    chart = ChartWorkspace(dict(THEMES[DEFAULT_THEME_NAME]), use_opengl=False)
    chart.resize(1920, 1000)
    chart.show()
    seconds = INTERVAL_SECONDS[chart.interval]
    rows = [Candle(1_700_000_000 + index * seconds, 100 + math.sin(index / 31),
                   102 + math.sin(index / 31), 98 + math.sin(index / 31),
                   100.1 + math.sin(index / 31), 10 + index % 10, 1000)
            for index in range(5800)]
    chart._commit_snapshot(prepare_snapshot({"interval": chart.interval, "candles": rows}))
    chart.set_order_rail_market_symbol("BTCUSDT")
    chart.set_working_orders([
        {"symbol": "BTCUSDT", "orderId": index + 1, "_source": "STANDARD",
         "side": "BUY" if index % 2 else "SELL", "type": "LIMIT",
         "price": str(98.5 + index * .15), "origQty": "1", "executedQty": "0",
         "status": "NEW", "positionSide": "BOTH"}
        for index in range(orders)
    ])
    assert len(chart._active_parked_order_rails()) == orders
    for _ in range(12):
        app.processEvents()
    chart._begin_interaction_priority()
    chart._position_interaction_overlays()
    return chart


def rail_geometry(chart):
    return [{"hud": list(item["hud"].geometry().getRect()),
             "line": list(item["visual"].geometry().getRect()),
             "mask": list(item["visual"].mask().boundingRect().getRect()),
             "anchor": item["hud"].line_anchor_y,
             "offscreen": item["hud"].offscreen_direction,
             "price": item["hud"].price_text,
             "visible": (item["hud"].isVisible(), item["visual"].isVisible())}
            for item in chart._active_parked_order_rails()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--overlay-samples", type=int, default=500)
    parser.add_argument("--capture-dir")
    args = parser.parse_args()
    app = QtWidgets.QApplication([])
    results = {"source": str(SOURCE), "environment": {"python": platform.python_version(),
               "qt": QtCore.qVersion(), "platform": platform.platform(),
               "qt_platform": app.platformName(), "cpu_count": os.cpu_count()},
               "limits": "Synthetic warmed GUI callbacks; excludes live feeds, display presentation and GPU throughput.",
               "watchlist": [], "overlays": []}
    captures = Path(args.capture_dir) if args.capture_dir else None
    if captures:
        captures.mkdir(parents=True, exist_ok=True)
    try:
        for count in (50, 200):
            source = market_widgets.WatchlistWidget(dict(THEMES[DEFAULT_THEME_NAME]))
            source.set_symbols([f"COIN{index:03d}USDT" for index in range(count)])
            sidebar = market_widgets.WatchlistSidebarWidget(source)
            sidebar.resize(420, 1000)
            sidebar.show()
            app.processEvents()
            now = [10_000.0]
            for index, symbol in enumerate(source.symbols):
                source.move_samples[symbol] = deque(
                    [(now[0] - 1795 + offset * 5, 100 + index + math.sin(offset / 19) * .1)
                     for offset in range(360)], maxlen=420)
            def update(index):
                now[0] += 1
                source.hour_changes = {symbol: index * 1.13 for symbol in source.symbols}
                source.update_tickers([{"s": symbol, "c": str(100 + coin + (index + 1) * 1.37),
                                        "P": str(index * 1.73), "q": "30000000"}
                                       for coin, symbol in enumerate(source.symbols)])
            with patch.object(market_widgets, "time", SimpleNamespace(monotonic=lambda: now[0])):
                update(-1)  # Cache initialization is outside warmed samples.
                result = measure(update, max(3, args.samples))
            app.processEvents()
            results["watchlist"].append({"symbols": count, "all_displayed_values_change": result,
                                          "column_widths": [sidebar.table.columnWidth(i) for i in range(5)]})
            if captures:
                sidebar.grab().save(str(captures / f"watchlist-{count}.png"))
            sidebar.close()
            source.close()
            sidebar.deleteLater()
            source.deleteLater()
            app.processEvents()

        for orders in (0, 10, 20):
            chart = chart_fixture(app, orders)
            sample_count = max(10, args.overlay_samples)
            stable = measure(lambda index: chart._position_interaction_overlays(), sample_count)
            x0, x1 = chart.price_plot.viewRange()[0]
            durations = []
            for index in range(sample_count):
                delta = (index % 11 - 5) * INTERVAL_SECONDS[chart.interval] * .1
                chart.price_plot.setXRange(x0 + delta, x1 + delta, padding=0)
                started = time.perf_counter()
                chart._position_interaction_overlays()
                durations.append((time.perf_counter() - started) * 1000)
            ordered = sorted(durations)
            moving = {"samples": sample_count, "median_ms": round(statistics.median(durations), 3),
                      "p95_ms": round(ordered[math.ceil(sample_count * .95) - 1], 3),
                      "max_ms": round(max(durations), 3)}
            chart.price_plot.setXRange(x0, x1, padding=0)
            chart._position_interaction_overlays()
            results["overlays"].append({"orders": orders, "stable_commit": stable, "pan_commit": moving})
            if orders == 20:
                results["rail_geometry"] = {}
                for scenario, logarithmic, scale, size in (
                    ("linear", False, (97, 104), (1920, 1000)),
                    ("zoomed", False, (99, 101), (1280, 800)),
                    ("logarithmic", True, (math.log10(97), math.log10(104)), (1920, 1000)),
                ):
                    chart.set_logarithmic(logarithmic)
                    chart.resize(*size)
                    chart.price_plot.setYRange(*scale, padding=0)
                    for _ in range(3):
                        app.processEvents()
                    chart._order_rail_phase = 0.0
                    chart._position_interaction_overlays()
                    results["rail_geometry"][scenario] = rail_geometry(chart)
                    if captures:
                        chart.grab().save(str(captures / f"chart-{scenario}.png"))
            chart.set_presentation_active(False)
            chart._interaction_gc.set_active(id(chart), False)
            chart.close()
            chart.deleteLater()
            app.processEvents()
    finally:
        shutdown_analysis()
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps({key: results[key] for key in ("environment", "watchlist", "overlays")}, indent=2))


if __name__ == "__main__":
    main()
