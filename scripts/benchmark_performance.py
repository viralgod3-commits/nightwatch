"""Offline warm-call benchmarks; no credentials, network, or live orders.

Run with QT_QPA_PLATFORM=offscreen. NIGHTWATCH_BENCH_SOURCE optionally selects
an earlier source checkout, so the same harness measures both revisions.
"""
import argparse
import inspect
import json
import os
from pathlib import Path
import pickle
import platform
import statistics
import sys
import time

SOURCE = Path(os.environ.get("NIGHTWATCH_BENCH_SOURCE", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(SOURCE))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.pop("BINANCE_API_KEY", None)
os.environ.pop("BINANCE_API_SECRET", None)

from PySide6 import QtCore, QtWidgets
from nightwatch.models import Candle
from nightwatch.leadership import (
    HOUR, LeadershipTimelineWidget, SectorOverviewWidget, _prepare_leaders,
    _workspace_analysis, _SECTOR_OVERVIEW_SECTOR_ORDER,
)
from nightwatch.chart.analysis import shutdown_analysis
from nightwatch.trading.trading_ui import (
    WorkingOrderRow, _new_account_card_list, _populate_account_cards,
)


def measure(function, samples):
    values = []
    for _ in range(samples):
        before = time.perf_counter()
        function()
        values.append((time.perf_counter() - before) * 1000)
    return {"samples": samples, "median_ms": round(statistics.median(values), 3),
            "max_ms": round(max(values), 3), "samples_ms": [round(x, 3) for x in values]}


def dataset(count):
    symbols = tuple(f"COIN{index:03d}USDT" for index in range(count))
    end = 200 * HOUR
    histories = {symbol: {index * HOUR: Candle((index - 1) * HOUR / 1000,
                 100 + index * .1, 101 + index * .1, 99 + index * .1,
                 100 + index * .1 + (coin % 9) * .02, 10, 1000 + coin)
                 for index in range(1, 201)} for coin, symbol in enumerate(("BTCUSDT", *symbols))}
    return dict(symbols=symbols, series=histories, spot_series={}, cursor=end, hours=24,
                categories={}, query="", sort_mode="state", tickers={}, valid_symbols=frozenset())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--samples", type=int, default=12)
    options = parser.parse_args()
    samples = max(3, options.samples)
    app = QtWidgets.QApplication([])
    results = {"source": str(SOURCE), "environment": {"python": platform.python_version(),
               "qt": QtCore.qVersion(), "platform": platform.platform(),
               "cpu_count": os.cpu_count(), "fonts": "system fallback", "qt_platform": "offscreen"},
               "limits": "Synthetic warmed callbacks/IPC, excludes actual painting, monitor presentation, live feeds and selected-coin detail work.",
               "leaders": [], "orders": []}
    try:
        for count in (40, 100, 200):
            state = dataset(count)
            widget = LeadershipTimelineWidget({})
            widget.symbols = list(state["symbols"])
            widget._render_facts = lambda: None
            widget._render_changes = lambda *args: None
            widget._schedule_details = lambda: None
            prepared = _prepare_leaders(state)
            widget._render(prepared)
            commit = measure(lambda: widget._render(prepared), samples)
            reversed_order = list(reversed(prepared["ordered"]))
            reorders = iter(range(samples))
            def reorder():
                order = reversed_order if next(reorders) % 2 == 0 else prepared["ordered"]
                widget._render({**prepared, "ordered": order})
            reordered = measure(reorder, samples)
            transport = getattr(widget, "_history_transport", None)
            def analyze(view):
                if transport is not None:
                    return _workspace_analysis(_prepare_leaders, view, transport)
                return _workspace_analysis(_prepare_leaders, view)
            assert analyze(state) == prepared  # Spawn and full seed are outside warm measurements.
            views = iter(range(samples))
            def filter_view():
                view = {**state, "query": "COIN0" if next(views) % 2 else "", "sort_mode": "pair"}
                return analyze(view)
            filtered = measure(filter_view, samples)
            if transport is not None:
                from nightwatch.research import history_update, HISTORY_FIELDS
                histories = {name: state[name] for name in HISTORY_FIELDS if name in state}
                view = {name: value for name, value in state.items() if name not in histories}
                message = (history_update(histories, histories), view)
            else:
                message = state
            results["leaders"].append({"symbols": count, "history_bars_per_symbol": 200,
                "gui_unchanged": commit, "gui_reorder": reordered, "warm_filter_roundtrip": filtered,
                "warm_input_pickle": measure(lambda: pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL), samples),
                "warm_input_bytes": len(pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL))})
            widget.shutdown()
            widget.deleteLater()
            app.processEvents()

        supports_context = "update_context" in inspect.signature(_populate_account_cards).parameters
        for count in (20, 100, 200):
            view = _new_account_card_list()
            rows = [{"symbol": f"COIN{index:03d}USDT", "orderId": index + 1, "_source": "ORDER",
                     "side": "BUY", "type": "LIMIT", "price": "100", "origQty": "1",
                     "executedQty": "0", "status": "NEW", "positionSide": "BOTH"}
                    for index in range(count)]
            factory = lambda row: WorkingOrderRow(row, {}, "COIN000USDT")
            update = lambda card, row: card.update_payload(row, "COIN000USDT")
            def populate(payloads):
                kwargs = {"update_context": "COIN000USDT"} if supports_context else {}
                return _populate_account_cards(view, payloads, factory, "No orders", update_existing=update, **kwargs)
            populate(rows)
            unchanged = measure(lambda: populate(rows), samples)
            record = {"orders": count, "unchanged_update": unchanged}
            for changed_count, label in ((0, "unchanged_reorder"), (min(10, count), "ten_changed_reorder"), (count, "all_changed_reorder")):
                turns = iter(range(samples))
                def reorder_orders():
                    turn = next(turns)
                    refreshed = [{**row, "price": str(101 + turn)} if index < changed_count else row
                                 for index, row in enumerate(rows)]
                    populate(list(reversed(refreshed)) if turn % 2 == 0 else refreshed)
                record[label] = measure(reorder_orders, samples)
                app.processEvents()
            results["orders"].append(record)
            view.deleteLater()
            app.processEvents()

        widget = SectorOverviewWidget({})
        metrics = {sector: dict(members=10, performance=index, trend=[1, None, 2],
                   outperformers=3, covered=10, volume_share=10, previous_volume_share=8)
                   for index, sector in enumerate(_SECTOR_OVERVIEW_SECTOR_ORDER)}
        performances = {frame: {sector: 1 for sector in metrics} for frame in ("15m", "1h", "4h", "1d")}
        widget._render_table(metrics, performances)
        results["sector_table"] = measure(lambda: widget._render_table(metrics, performances), samples)
        widget.shutdown()
        widget.deleteLater()
        app.processEvents()
    finally:
        shutdown_analysis()
    target = Path(options.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
