from __future__ import annotations

import math
import random
from collections import deque

import numpy as np
import pytest
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

from nightwatch.ui import market_widgets
from nightwatch.ui.market_widgets import (
    WatchlistSidebarWidget,
    WatchlistWidget,
    _WatchlistMoveHistory,
)


THEME = {
    "bg": "#11151c",
    "panel2": "#1a2029",
    "active": "#263342",
    "green": "#56d6a0",
    "red": "#f07178",
    "cyan": "#63d9e8",
    "text": "#d7e0ed",
    "muted": "#8592a6",
    "border": "#394453",
}


def legacy_move_state(
    samples: deque[tuple[float, float]],
    signal_until: dict[str, float],
    signal_direction: dict[str, int],
    last_trigger: dict[str, float],
    symbol: str,
    ticker: dict,
    now: float,
) -> None:
    """Reference implementation copied from the pre-cache watchlist detector."""
    price = market_widgets.safe_float(ticker.get("c"))
    if price <= 0:
        return
    if samples and now - samples[-1][0] < 5.0:
        samples[-1] = (now, price)
    else:
        samples.append((now, price))
    cutoff = now - 1_800.0
    while samples and samples[0][0] < cutoff:
        samples.popleft()
    reference = next(
        ((stamp, value) for stamp, value in samples if stamp >= now - 300.0),
        samples[0],
    )
    if now - reference[0] < 270.0 or reference[1] <= 0:
        return
    move = (price / reference[1] - 1.0) * 100.0
    historical: list[float] = []
    rows = list(samples)
    start = 0
    for end in range(len(rows)):
        while start < end and rows[end][0] - rows[start][0] > 330.0:
            start += 1
        if 270.0 <= rows[end][0] - rows[start][0] <= 330.0 and rows[start][1] > 0:
            historical.append(abs((rows[end][1] / rows[start][1] - 1.0) * 100.0))
    baseline = float(np.median(historical[:-1])) if len(historical) > 2 else 0.0
    quote_volume = market_widgets.safe_float(ticker.get("q"))
    liquidity_floor = (
        0.7
        if quote_volume >= 1_000_000_000
        else 1.1
        if quote_volume >= 100_000_000
        else 1.7
        if quote_volume >= 20_000_000
        else 2.4
    )
    threshold = max(liquidity_floor, baseline * 3.0)
    if abs(move) < threshold or now - last_trigger.get(symbol, 0.0) < 30.0:
        return
    last_trigger[symbol] = now
    signal_until[symbol] = now + 30.0
    signal_direction[symbol] = 1 if move > 0 else -1


def legacy_signal(
    now: float, symbol: str, signal_until: dict[str, float], signal_direction: dict[str, int]
) -> int:
    if signal_until.get(symbol, 0.0) <= now:
        return 0
    return signal_direction.get(symbol, 0)


@pytest.mark.parametrize(
    "rows",
    [
        [(0.0, 100.0), (270.0, 101.0), (540.0, 102.0), (810.0, 103.0)],
        [(0.0, 100.0), (330.0, 110.0), (660.0, 100.0), (990.0, 105.0)],
        [(0.0, 100.0), (300.0, 101.0), (300.0, 102.0), (600.0, 104.0)],
        [(1_000.0, 100.0), (1_400.0, 102.0), (1_200.0, 101.0), (1_700.0, 99.0)],
        [(0.0, 100.0), (600.0, 101.0), (1_200.0, 103.0), (2_000.0, 105.0)],
    ],
)
def test_move_history_rebuild_matches_legacy_boundaries_and_irregular_timestamps(rows):
    samples = deque(rows, maxlen=420)
    cache = _WatchlistMoveHistory(samples)
    expected = []
    start = 0
    for end in range(len(rows)):
        while start < end and rows[end][0] - rows[start][0] > 330.0:
            start += 1
        if 270.0 <= rows[end][0] - rows[start][0] <= 330.0 and rows[start][1] > 0:
            expected.append(abs((rows[end][1] / rows[start][1] - 1.0) * 100.0))
    expected_baseline = float(np.median(expected[:-1])) if len(expected) > 2 else 0.0
    assert cache.baseline == expected_baseline
    expected_metrics = []
    for index, row in enumerate(rows):
        start_row = rows[cache.starts[index]]
        if 270.0 <= row[0] - start_row[0] <= 330.0 and start_row[1] > 0:
            expected_metrics.append(abs((row[1] / start_row[1] - 1.0) * 100.0))
        else:
            expected_metrics.append(None)
    assert cache.metrics == expected_metrics


def test_detector_cache_preserves_signals_cooldowns_pruning_and_external_replacement(
    qapp, monkeypatch
):
    del qapp
    now = [10_000.0]
    monkeypatch.setattr(market_widgets.time, "monotonic", lambda: now[0])
    source = WatchlistWidget(THEME)
    symbol = "BTCUSDT"
    legacy_samples: dict[str, deque[tuple[float, float]]] = {}
    legacy_until: dict[str, float] = {}
    legacy_direction: dict[str, int] = {}
    legacy_trigger: dict[str, float] = {}
    rng = random.Random(991)
    price = 100.0

    for index in range(1_050):
        if index and index % 223 == 0:
            now[0] += 1_810.0  # Exercise stale-history pruning and expired flags.
        elif index and index % 7 == 2:
            now[0] += 1.25  # Replace the newest open sample.
        else:
            now[0] += 5.0
        if index % 61 == 0:
            price *= 1.009 if index % 122 == 0 else 0.991
        else:
            price *= 1.0 + rng.uniform(-0.0007, 0.0007)
        payload = {
            "c": str(price),
            "q": ("1200000000" if index % 3 == 0 else "80000000" if index % 3 == 1 else "10000000"),
        }
        if index % 137 == 0:
            payload["c"] = "not-a-price"

        source._observe_move(symbol, payload)
        samples = legacy_samples.setdefault(symbol, deque(maxlen=420))
        legacy_move_state(
            samples,
            legacy_until,
            legacy_direction,
            legacy_trigger,
            symbol,
            payload,
            now[0],
        )
        assert list(source.move_samples.get(symbol, ())) == list(samples)
        assert source.move_signal(symbol) == legacy_signal(
            now[0], symbol, legacy_until, legacy_direction
        )
        assert source.move_last_trigger.get(symbol, 0.0) == legacy_trigger.get(symbol, 0.0)

    old_deque = source.move_samples[symbol]
    seeded = deque(
        [(now[0] - 500.0 + i * 2.0, 100.0 + (i % 5)) for i in range(420)],
        maxlen=420,
    )
    seeded_first = seeded[0]
    source.move_samples = {symbol: seeded}
    legacy_samples[symbol] = deque(seeded, maxlen=420)
    now[0] = seeded[-1][0] + 5.1  # The next append forces maxlen eviction.
    payload = {"c": "101.5", "q": "1000000000"}
    source._observe_move(symbol, payload)
    legacy_move_state(
        legacy_samples[symbol],
        legacy_until,
        legacy_direction,
        legacy_trigger,
        symbol,
        payload,
        now[0],
    )
    assert len(source.move_samples[symbol]) == 420
    assert source.move_samples[symbol][0] != seeded_first
    assert source._move_history_cache[symbol].source is source.move_samples[symbol]
    assert list(source.move_samples[symbol]) == list(legacy_samples[symbol])
    assert source.move_signal(symbol) == legacy_signal(
        now[0], symbol, legacy_until, legacy_direction
    )
    assert old_deque is not source.move_samples[symbol]

    source.set_symbols(["ETHUSDT"])
    assert symbol not in source._move_history_cache
    assert symbol in source.move_samples  # Detector history and cooldown state persist.
    source.deleteLater()


def _natural_numeric_widths(sidebar: WatchlistSidebarWidget, qapp) -> tuple[int, int, int]:
    header = sidebar.table.horizontalHeader()
    for column in (2, 3, 4):
        header.setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
    qapp.processEvents()
    widths = tuple(header.sectionSize(column) for column in (2, 3, 4))
    for column in (2, 3, 4):
        header.setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeMode.Fixed)
    for column in (2, 3, 4):
        sidebar.table.resizeColumnToContents(column)
    qapp.processEvents()
    return widths


def test_sidebar_batch_sizing_keeps_natural_widths_and_ui_state(qapp, monkeypatch):
    monkeypatch.setattr(market_widgets, "coin_icon_exists", lambda _symbol: True)
    monkeypatch.setattr(
        market_widgets, "watchlist_coin_icon", lambda _symbol, _theme: QtGui.QIcon()
    )
    symbols = [f"TOKEN{index:03d}USDT" for index in range(200)]
    source = WatchlistWidget(THEME)
    source.set_symbols(symbols, emit=False)
    source.current_symbol = ""
    source.tickers = {
        symbol: {
            "s": symbol,
            "c": 100.0 + index,
            "P": (index - 100) / 7.0,
        }
        for index, symbol in enumerate(symbols)
    }
    source.hour_changes = {
        symbol: (index - 100) / 11.0 for index, symbol in enumerate(symbols)
    }
    sidebar = WatchlistSidebarWidget(source)
    sidebar.resize(430, 260)
    sidebar.show()
    qapp.processEvents()

    header = sidebar.table.horizontalHeader()
    assert all(
        header.sectionResizeMode(column) == QtWidgets.QHeaderView.ResizeMode.Fixed
        for column in (2, 3, 4)
    )
    assert tuple(header.sectionSize(column) for column in (2, 3, 4)) == _natural_numeric_widths(
        sidebar, qapp
    )

    resize_calls = []
    original_resize = sidebar.table.resizeColumnToContents

    def counted_resize(column):
        resize_calls.append(column)
        return original_resize(column)

    sidebar.table.resizeColumnToContents = counted_resize
    sidebar.select_symbol(symbols[175])
    sidebar.table.verticalScrollBar().setValue(sidebar.table.verticalScrollBar().maximum())
    qapp.processEvents()
    selected = sidebar.selected_symbol()
    scroll = sidebar.table.verticalScrollBar().value()
    assert selected == symbols[175]
    assert scroll > 0

    for index, symbol in enumerate(symbols):
        source.tickers[symbol] = {
            "s": symbol,
            "c": 12_345.6 if index == 0 else 1.0 + index / 1_000.0,
            "P": 250.0 if index == 0 else (index - 100) / 100.0,
        }
        source.hour_changes[symbol] = 125.0 if index == 0 else (index - 100) / 100.0
    sidebar.refresh()
    qapp.processEvents()
    assert resize_calls == [2, 3, 4]
    assert sidebar.selected_symbol() == selected
    assert sidebar.table.verticalScrollBar().value() == scroll
    assert sidebar.table.item(0, 2).text() == "12,345.6"
    assert sidebar.table.item(0, 3).text() == "+125.00%"
    assert sidebar.table.item(0, 4).text() == "+250.00%"
    expected = _natural_numeric_widths(sidebar, qapp)
    assert tuple(header.sectionSize(column) for column in (2, 3, 4)) == expected

    resize_calls.clear()
    sidebar.refresh()
    assert resize_calls == []

    # Shorter replacements must shrink to the same natural Qt widths.
    for symbol in symbols:
        source.tickers[symbol] = {"s": symbol, "c": 1.0, "P": 0.0}
        source.hour_changes[symbol] = 0.0
    sidebar.refresh()
    assert resize_calls == [2, 3, 4]
    shrunk = tuple(header.sectionSize(column) for column in (2, 3, 4))
    assert shrunk == _natural_numeric_widths(sidebar, qapp)
    assert shrunk[0] <= expected[0] and shrunk[1] <= expected[1] and shrunk[2] <= expected[2]

    resize_calls.clear()
    source.set_groups({"Main": symbols, "Alt": ["ALTUSDT"]}, "Main", emit=False)
    sidebar._groups_changed()
    assert sidebar.group_selector.currentText() == "Main"
    source.set_active_group("Alt")
    assert sidebar.group_selector.currentText() == "Alt"
    assert sidebar.table.rowCount() == 1
    source.set_active_group("Main")
    assert sidebar.group_selector.currentText() == "Main"
    assert sidebar.table.rowCount() == 200

    # Theme, typography and post-style/DPI events all recompute once per dirty
    # numeric column before the next paint/event-loop screenshot.
    resize_calls.clear()
    themed = {**THEME, "text": "#ffffff", "active": "#304050"}
    source.apply_theme(themed)
    sidebar.apply_theme(themed)
    assert resize_calls[-3:] == [2, 3, 4]
    assert tuple(header.sectionSize(column) for column in (2, 3, 4)) == _natural_numeric_widths(
        sidebar, qapp
    )

    resize_calls.clear()
    large_font = QtGui.QFont("DejaVu Sans", 16)
    monkeypatch.setattr(
        market_widgets,
        "typography_font",
        lambda role: QtGui.QFont(large_font) if role == market_widgets.TextRole.TABLE_VALUE else QtGui.QFont(large_font),
    )
    sidebar._typography_changed()
    assert resize_calls[-3:] == [2, 3, 4]
    assert sidebar.table.item(0, 2).font().pointSize() == 16
    assert tuple(header.sectionSize(column) for column in (2, 3, 4)) == _natural_numeric_widths(
        sidebar, qapp
    )

    resize_calls.clear()
    QtCore.QCoreApplication.sendEvent(
        sidebar.watchlist_header,
        QtCore.QEvent(QtCore.QEvent.Type.ScreenChangeInternal),
    )
    assert resize_calls[-3:] == [2, 3, 4]
    assert all(not math.isnan(float(header.sectionSize(column))) for column in (2, 3, 4))

    sidebar.close()
    source.deleteLater()
