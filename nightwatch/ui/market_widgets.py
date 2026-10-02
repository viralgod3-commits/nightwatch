"""Market statistics, metric history, watchlists, and symbol-selection widgets."""

from __future__ import annotations

import html
import math
import time
from bisect import bisect_left, insort_left
from collections import deque
from datetime import datetime, timezone
from typing import Any

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt, Signal

from ..constants import (
    DEFAULT_MARKET_BAR_TIMEFRAMES, DEFAULT_SYMBOL, MARKET_SORT_MODES,
    normalized_market_bar_timeframes,
)
from ..coin_catalog import coin_base_symbol, coin_icon_bytes, coin_icon_exists
from ..networking.binance import launch_task
from ..utilities import ElidedLabel, alpha_color, device_pixel_rect, line_icon
from ..utilities import (
    TextRole,
    apply_text_render_hints,
    set_text_role,
    typography_controller,
    typography_font,
)
from ..models import (
    format_price,
    human_number,
    parse_compact_amount,
    perpetual_display_symbol,
    safe_float,
)


_WATCHLIST_ICON_CACHE: dict[tuple[str, int, str, str, str], QtGui.QIcon] = {}


_WATCHLIST_WIDTH_ENVIRONMENT_EVENTS = frozenset(
    event_type
    for name in (
        "FontChange",
        "ApplicationFontChange",
        "StyleChange",
        "ScreenChangeInternal",
        "DevicePixelRatioChange",
        "LayoutDirectionChange",
        "ApplicationLayoutDirectionChange",
    )
    if (event_type := getattr(QtCore.QEvent.Type, name, None)) is not None
)


def _watchlist_width_environment_changed(event: QtCore.QEvent) -> bool:
    return event.type() in _WATCHLIST_WIDTH_ENVIRONMENT_EVENTS


class _WatchlistMoveHistory:
    """Cached five-minute history used by the watchlist move detector.

    Appends and updates to the newest sample leave all earlier five-minute
    moves unchanged. Keep those moves in order and maintain the closed-history
    median inputs incrementally. Prefix eviction repairs affected references;
    new or irregular histories rebuild the bounded 420-sample window with NumPy.
    """

    def __init__(self, samples: deque[tuple[float, float]]):
        self.source = samples
        self.rows = list(samples)
        self.timestamps: list[float] = []
        self.starts: list[int] = []
        self.metrics: list[float | None] = []
        self.valid_indices: list[int] = []
        self.baseline_values: list[float] = []
        self.monotonic = True
        self.incremental_safe = True
        self.baseline = 0.0
        self._rebuild()

    @staticmethod
    def _move(start: tuple[float, float], end: tuple[float, float]) -> float | None:
        span = end[0] - start[0]
        if 270.0 <= span <= 330.0 and start[1] > 0:
            return abs((end[1] / start[1] - 1.0) * 100.0)
        return None

    def _rebuild(self) -> None:
        count = len(self.rows)
        self.starts = [0] * count
        self.metrics = [None] * count
        self.valid_indices = []
        self.baseline_values = []
        self.monotonic = all(
            self.rows[index][0] >= self.rows[index - 1][0]
            for index in range(1, count)
        )

        if count and self.monotonic:
            stamps = np.fromiter((row[0] for row in self.rows), dtype=np.float64, count=count)
            self.timestamps = [float(value) for value in stamps]
            prices = np.fromiter((row[1] for row in self.rows), dtype=np.float64, count=count)
            starts = np.searchsorted(stamps, stamps - 330.0, side="left")
            starts = np.maximum.accumulate(starts)
            spans = stamps - stamps[starts]
            valid = (spans >= 270.0) & (spans <= 330.0) & (prices[starts] > 0)
            with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
                magnitudes = np.abs((prices / prices[starts] - 1.0) * 100.0)
            self.starts = [int(value) for value in starts]
            self.metrics = [
                float(magnitudes[index]) if valid[index] else None
                for index in range(count)
            ]
        elif count:
            # Seeded or malformed histories can contain decreasing timestamps.
            # Keep the detector's original forward-only sliding-window rule.
            start = 0
            for end in range(count):
                self.timestamps.append(self.rows[end][0])
                while start < end and self.rows[end][0] - self.rows[start][0] > 330.0:
                    start += 1
                self.starts[end] = start
                self.metrics[end] = self._move(self.rows[start], self.rows[end])

        self.valid_indices = [
            index for index, metric in enumerate(self.metrics) if metric is not None
        ]
        if self.valid_indices:
            last_valid = self.valid_indices[-1]
            self.baseline_values = [
                float(metric)
                for index, metric in enumerate(self.metrics)
                if metric is not None and index != last_valid
            ]
            self.baseline_values.sort()
        self.incremental_safe = not any(
            metric is not None and math.isnan(metric) for metric in self.metrics
        )
        self._refresh_baseline()

    def _refresh_baseline(self) -> None:
        # The old detector used a zero baseline until it had at least three
        # valid historical moves, then took the median excluding the newest.
        if len(self.valid_indices) <= 2:
            self.baseline = 0.0
            return
        if not self.incremental_safe:
            historical = [
                metric for metric in self.metrics if metric is not None
            ]
            self.baseline = float(np.median(historical[:-1]))
            return
        middle = len(self.baseline_values) // 2
        if len(self.baseline_values) % 2:
            self.baseline = self.baseline_values[middle]
        else:
            # NumPy median averages the middle pair with np.mean. Retain the
            # same floating-point reduction while avoiding a full sort/copy.
            self.baseline = float(
                np.mean((self.baseline_values[middle - 1], self.baseline_values[middle]))
            )

    def matches(self, samples: deque[tuple[float, float]]) -> bool:
        if self.source is not samples or len(self.rows) != len(samples):
            return False
        if not samples:
            return not self.rows
        return self.rows[0] == samples[0] and self.rows[-1] == samples[-1]

    def can_update(
        self,
        samples: deque[tuple[float, float]],
        sample: tuple[float, float],
        *,
        replace: bool,
    ) -> bool:
        if not self.matches(samples) or not self.monotonic or not self.incremental_safe:
            return False
        if replace:
            return len(self.rows) < 2 or sample[0] >= self.rows[-2][0]
        return not self.rows or sample[0] >= self.rows[-1][0]

    def _remove_baseline_value(self, value: float) -> None:
        index = bisect_left(self.baseline_values, value)
        if index < len(self.baseline_values) and self.baseline_values[index] == value:
            self.baseline_values.pop(index)

    def append(self, sample: tuple[float, float]) -> None:
        index = len(self.rows)
        old_latest = self.valid_indices[-1] if self.valid_indices else None
        self.rows.append(sample)
        self.timestamps.append(sample[0])
        start = self.starts[-1] if self.starts else 0
        while start < index and sample[0] - self.rows[start][0] > 330.0:
            start += 1
        self.starts.append(start)
        metric = self._move(self.rows[start], sample)
        self.metrics.append(metric)
        if metric is not None:
            if old_latest is not None:
                insort_left(self.baseline_values, float(self.metrics[old_latest]))
            self.valid_indices.append(index)
            if math.isnan(metric):
                self.incremental_safe = False
        self._refresh_baseline()

    def replace_last(self, sample: tuple[float, float]) -> None:
        index = len(self.rows) - 1
        old_latest = self.valid_indices[-1] if self.valid_indices else None
        old_metric = self.metrics[index]
        if old_metric is not None:
            self.valid_indices.pop()

        self.rows[index] = sample
        self.timestamps[index] = sample[0]
        start = self.starts[index - 1] if index else 0
        while start < index and sample[0] - self.rows[start][0] > 330.0:
            start += 1
        self.starts[index] = start
        metric = self._move(self.rows[start], sample)
        self.metrics[index] = metric
        if metric is not None:
            self.valid_indices.append(index)
            if math.isnan(metric):
                self.incremental_safe = False

        new_latest = self.valid_indices[-1] if self.valid_indices else None
        if new_latest != old_latest:
            if new_latest is not None and new_latest != index:
                self._remove_baseline_value(float(self.metrics[new_latest]))
            if old_latest is not None and old_latest != index:
                insort_left(self.baseline_values, float(self.metrics[old_latest]))
        self._refresh_baseline()

    def prune_prefix(self, count: int) -> None:
        """Drop oldest samples and recompute only windows they could affect."""
        count = max(0, min(int(count), len(self.rows)))
        if not count:
            return
        old_latest = self.valid_indices[-1] if self.valid_indices else None
        old_rows = self.rows
        old_starts = self.starts
        old_metrics = self.metrics
        rows = old_rows[count:]
        starts = [max(0, start - count) for start in old_starts[count:]]
        metrics = old_metrics[count:]
        start = 0
        affected_old_indices: set[int] = set()
        for index, row in enumerate(rows):
            old_index = index + count
            old_start = old_starts[old_index]
            if old_start >= count:
                break
            while start < index and row[0] - rows[start][0] > 330.0:
                start += 1
            starts[index] = start
            metrics[index] = self._move(rows[start], row)
            affected_old_indices.add(old_index)

        valid_indices = [
            index for index, metric in enumerate(metrics) if metric is not None
        ]
        new_latest = valid_indices[-1] if valid_indices else None
        changed_identities = set(range(min(count, len(old_metrics))))
        changed_identities.update(affected_old_indices)
        if old_latest is not None:
            changed_identities.add(old_latest)
        if new_latest is not None:
            changed_identities.add(new_latest + count)
        for old_index in changed_identities:
            old_metric = old_metrics[old_index] if old_index < len(old_metrics) else None
            new_index = old_index - count
            new_metric = metrics[new_index] if 0 <= new_index < len(metrics) else None
            old_member = old_metric is not None and old_index != old_latest
            new_member = new_metric is not None and new_index != new_latest
            if old_member and (not new_member or old_metric != new_metric):
                self._remove_baseline_value(float(old_metric))
            if new_member and (not old_member or old_metric != new_metric):
                insort_left(self.baseline_values, float(new_metric))

        self.rows = rows
        self.timestamps = self.timestamps[count:]
        self.starts = starts
        self.metrics = metrics
        self.valid_indices = valid_indices
        self._refresh_baseline()

    def reference(self, now: float) -> tuple[float, float] | None:
        if not self.rows:
            return None
        if not self.monotonic:
            return next(
                (row for row in self.rows if row[0] >= now - 300.0),
                self.rows[0],
            )
        index = bisect_left(self.timestamps, now - 300.0)
        return self.rows[index] if index < len(self.rows) else self.rows[0]


COMPACT_METRIC_HEIGHT = 36
COMPACT_METRIC_GAP = 1


COMPACT_EQUAL_METRIC_WIDTH = 104


COMPACT_IDENTITY_WIDTH = 160
COMPACT_PRIMARY_METRICS = ("volume", "oi", "long_short", "funding")
COMPACT_SECONDARY_METRICS: tuple[str, ...] = ("taker",)


FUNDING_COLOR_THRESHOLD_PCT = 0.08


def _compact_market_notional(value: float) -> str:
    """Show quote notionals with consistent precision and clean unit boundaries."""
    if not math.isfinite(value) or value < 0:
        return "—"
    scales = ((1.0, ""), (1e3, "K"), (1e6, "M"), (1e9, "B"), (1e12, "T"))
    index = max(i for i, (scale, _suffix) in enumerate(scales) if value >= scale) if value >= 1 else 0
    while True:
        scale, suffix = scales[index]
        shown = value / scale
        if not suffix:
            if round(shown, 2) >= 1000:
                index += 1
                continue
            text = f"{shown:.3g}" if 0 < shown < 1 else f"{shown:,.2f}"
            break
        decimals = 2
        if round(shown, decimals) >= 1000 and index < len(scales) - 1:
            index += 1
            continue
        text = f"{shown:.{decimals}f}"
        break
    if "." in text and "e" not in text:
        text = text.rstrip("0").rstrip(".")
    return f"${text}{suffix}"


def _market_funding_rate(value: float) -> str:
    if not math.isfinite(value):
        return "—"
    if value == 0:
        return "0%"
    decimals = 4
    if abs(value) < 0.0001:
        decimals = min(10, max(4, 2 - math.floor(math.log10(abs(value)))))
    if round(value, decimals) == 0:
        return f"{value:+.3g}%"
    return f"{value:+.{decimals}f}".rstrip("0").rstrip(".") + "%"


def _market_funding_time(next_ms: float, event_ms: float = 0.0) -> tuple[str, str]:
    """Return a compact countdown and exact UTC time from the exchange schedule."""
    if not math.isfinite(next_ms) or next_ms <= 0:
        return "", "—"
    try:
        exact = datetime.fromtimestamp(next_ms / 1000, timezone.utc).strftime("%d %b %H:%M UTC")
    except (OverflowError, OSError, ValueError):
        return "", "—"
    now_ms = event_ms if math.isfinite(event_ms) and event_ms > 0 else time.time() * 1000
    seconds = max(0, math.ceil((next_ms - now_ms) / 1000))
    if seconds == 0:
        return "due", exact
    if seconds < 60:
        return f"{seconds}s", exact
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours:02d}:{minutes:02d}", exact


class TimeframeStrip(QtWidgets.QWidget):
    """Always-visible compact chart-timeframe button strip."""

    activated = Signal(int)
    content_changed = Signal()


    BUTTON_WIDTH = 36
    NORMAL_SPACING = 2
    BUTTON_HEIGHT = 28

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self._items: list[tuple[str, Any]] = []
        self._buttons: list[QtWidgets.QPushButton] = []
        self._current_index = -1
        self._collapsed = False
        self._external_data: str | None = None

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(self.NORMAL_SPACING)
        layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self._group = QtWidgets.QButtonGroup(self)
        self._group.setExclusive(True)


        self._collapsed_button = QtWidgets.QPushButton(self)
        self._collapsed_button.setObjectName("timeframeStripButton")
        self._collapsed_button.setCheckable(True)
        self._collapsed_button.setChecked(True)
        self._collapsed_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._collapsed_button.setFixedSize(self.BUTTON_WIDTH, self.BUTTON_HEIGHT)
        self._collapsed_button.setAccessibleName("Current chart timeframe")
        set_text_role(self._collapsed_button, TextRole.UI_CONTROL_COMPACT)
        self._collapsed_button.clicked.connect(self._show_collapsed_menu)
        self._collapsed_button.hide()
        layout.addWidget(self._collapsed_button)

        self._collapsed_menu = QtWidgets.QMenu(self)
        self._collapsed_menu.installEventFilter(self)
        self._collapsed_menu.setToolTipsVisible(True)
        self._collapsed_actions = QtGui.QActionGroup(self)
        self._collapsed_actions.setExclusive(True)

        # A restored/CLI interval outside the favorites must still be visible.
        # It is not a saved favorite and does not take a numbered shortcut.
        self._external_button = QtWidgets.QPushButton(self)
        self._external_button.setObjectName("timeframeStripButton")
        self._external_button.setCheckable(True)
        self._external_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._external_button.setFixedSize(self.BUTTON_WIDTH, self.BUTTON_HEIGHT)
        self._external_button.setProperty("informationalToolTip", True)
        set_text_role(self._external_button, TextRole.UI_CONTROL_COMPACT)
        self._external_button.clicked.connect(self._show_collapsed_menu)
        self._external_button.hide()
        layout.addWidget(self._external_button)
        self._collapsed_button.setProperty("informationalToolTip", True)

    def addItem(self, text: str, user_data: Any = None) -> None:
        index = len(self._items)
        self._items.append((str(text), user_data))

        button = QtWidgets.QPushButton(str(text), self)
        button.setObjectName("timeframeStripButton")
        button.setCheckable(True)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setFixedSize(self.BUTTON_WIDTH, self.BUTTON_HEIGHT)
        button.setToolTip(f"{text} · Shortcut {index + 1}")
        button.setAccessibleName(f"{text} chart timeframe, shortcut {index + 1}")
        button.setProperty("informationalToolTip", True)
        set_text_role(button, TextRole.UI_CONTROL_COMPACT)
        button.clicked.connect(
            lambda _checked=False, i=index: self._activate_index(i)
        )
        self._group.addButton(button, index)
        self.layout().insertWidget(self.layout().count() - 1, button)
        button.setVisible(not self._collapsed)
        self._buttons.append(button)

        # Render the key without installing competing QAction shortcuts.
        action = self._collapsed_menu.addAction(f"{text}\t{index + 1}")
        action.setCheckable(True)
        action.setData(index)
        action.triggered.connect(
            lambda _checked=False, i=index: self._activate_index(i)
        )
        self._collapsed_actions.addAction(action)

        if self._current_index < 0:
            self.setCurrentIndex(0)

        self._sync_mode_width()

    def setItems(self, intervals: tuple[str, ...], current: str | None = None) -> None:
        current = str(current or self.currentData() or intervals[0])
        for button in self._buttons:
            self._group.removeButton(button)
            self.layout().removeWidget(button)
            button.hide()
            button.deleteLater()
        for action in self._collapsed_actions.actions():
            self._collapsed_actions.removeAction(action)
        self._collapsed_menu.clear()
        self._items.clear()
        self._buttons.clear()
        self._current_index = -1
        self._external_data = None
        for interval in intervals:
            text = interval.upper() if interval in {"1d", "1w"} else interval
            self.addItem(text, interval)
        self.setCurrentData(current)
        self.content_changed.emit()

    def count(self) -> int:
        return len(self._items)

    def itemText(self, index: int) -> str:
        if 0 <= index < len(self._items):
            return self._items[index][0]
        return ""

    def currentIndex(self) -> int:
        return self._current_index

    def currentData(self) -> Any:
        if 0 <= self._current_index < len(self._items):
            return self._items[self._current_index][1]
        return self._external_data

    def setCurrentData(self, value: str) -> None:
        index = self.findData(value)
        if index >= 0:
            self.setCurrentIndex(index)
            return
        self._current_index = -1
        self._external_data = str(value)
        self._group.setExclusive(False)
        for button in self._buttons:
            button.setChecked(False)
        self._group.setExclusive(True)
        for action in self._collapsed_actions.actions():
            action.setChecked(False)
        text = value.upper() if value in {"1d", "1w"} else value
        self._external_button.setText(text)
        self._external_button.setChecked(True)
        self._external_button.setToolTip("Current timeframe · outside your saved selection")
        self._external_button.setVisible(not self._collapsed)
        self._collapsed_button.setText(text)
        self._collapsed_button.setToolTip("Choose a saved timeframe")
        self._sync_mode_width()
        self.content_changed.emit()

    def findData(self, value: Any) -> int:
        for index, (_text, data) in enumerate(self._items):
            if data == value:
                return index
        return -1

    def setCurrentIndex(self, index: int) -> None:
        if index < 0 or index >= len(self._items):
            return
        was_external = self._external_data is not None
        self._external_data = None
        self._external_button.hide()
        self._current_index = int(index)
        for button_index, button in enumerate(self._buttons):
            checked = button_index == self._current_index
            if button.isChecked() != checked:
                blocker = QtCore.QSignalBlocker(button)
                button.setChecked(checked)
                del blocker
        for action_index, action in enumerate(self._collapsed_actions.actions()):
            action.setChecked(action_index == self._current_index)
        if 0 <= self._current_index < len(self._items):
            text = self._items[self._current_index][0]
            self._collapsed_button.setText(text)
            self._collapsed_button.setToolTip(f"Choose timeframe · Shortcuts 1–{self.count()}")
        if was_external:
            self._sync_mode_width()
            self.content_changed.emit()

    def expandedWidth(self, *, tight: bool | None = None) -> int:
        if tight is None:
            spacing = self.layout().spacing()
        else:
            spacing = 0 if tight else self.NORMAL_SPACING
        count = len(self._buttons) + int(self._external_data is not None)
        return count * self.BUTTON_WIDTH + max(0, count - 1) * max(0, int(spacing))

    def setTightSpacing(self, tight: bool) -> None:
        """Remove only inter-timeframe gaps during responsive width pressure."""
        spacing = 0 if bool(tight) else self.NORMAL_SPACING
        if self.layout().spacing() == spacing:
            return
        self.layout().setSpacing(spacing)
        self._sync_mode_width()

    def collapsedWidth(self) -> int:
        return self.BUTTON_WIDTH


    def setCollapsed(self, collapsed: bool) -> None:
        collapsed = bool(collapsed)
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self._collapsed_button.setVisible(collapsed)
        for button in self._buttons:
            button.setVisible(not collapsed)
        self._external_button.setVisible(not collapsed and self._external_data is not None)
        self._sync_mode_width()

    def _sync_mode_width(self) -> None:
        self.setFixedWidth(
            self.collapsedWidth() if self._collapsed else self.expandedWidth()
        )
        self.updateGeometry()

    def _show_collapsed_menu(self) -> None:


        self._collapsed_button.setChecked(True)
        self._external_button.setChecked(True)
        if not self._items:
            return
        anchor = self._collapsed_button if self._collapsed else self._external_button
        self._collapsed_menu.popup(
            anchor.mapToGlobal(QtCore.QPoint(0, anchor.height()))
        )

    def _activate_index(self, index: int) -> None:
        if index < 0 or index >= len(self._items):
            return
        self.setCurrentIndex(index)
        self.activated.emit(index)

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if (
            watched is self._collapsed_menu
            and event.type() == QtCore.QEvent.Type.KeyPress
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
        ):
            index = int(event.key()) - int(Qt.Key.Key_1)
            if 0 <= index < self.count():
                if not event.isAutoRepeat():
                    self._collapsed_menu.hide()
                    self._activate_index(index)
                event.accept()
                return True
        return super().eventFilter(watched, event)


METRIC_DETAIL_ORDER: tuple[tuple[str, str], ...] = (
    ("volume", "24H VOL"),
    ("taker", "TAKER"),
    ("oi", "OPEN INTEREST"),
    ("long_short", "LONG/SHORT"),
    ("funding", "FUNDING"),
)
METRIC_DETAIL_KEYS = {key for key, _label in METRIC_DETAIL_ORDER}


def invalidate_watchlist_coin_icon(symbol: str) -> None:
    """Drop only cached Qt icons for one base asset after its disk file changes."""
    base = coin_base_symbol(symbol)
    if not base:
        return
    for key in tuple(_WATCHLIST_ICON_CACHE):
        if key[0] == base:
            _WATCHLIST_ICON_CACHE.pop(key, None)


def watchlist_coin_icon(
    symbol: str,
    theme: dict[str, str] | None = None,
) -> QtGui.QIcon:
    """Return a DPR-aware icon loaded from assets/icons with an initials fallback."""
    base = coin_base_symbol(symbol)
    palette = theme or {}
    outline = str(palette.get("control_border", palette.get("border", "#586273")))
    surface = str(palette.get("control", palette.get("panel2", "#1B202A")))
    ink = str(palette.get("text", "#CED7E6"))
    screen = QtGui.QGuiApplication.primaryScreen()
    dpr = max(1.0, float(screen.devicePixelRatio()) if screen is not None else 1.0)
    dpr_key = max(1, int(round(dpr * 100.0)))
    cache_key = (base, dpr_key, outline, surface, ink)
    if cache_key in _WATCHLIST_ICON_CACHE:
        return _WATCHLIST_ICON_CACHE[cache_key]
    source = QtGui.QPixmap()
    payload = coin_icon_bytes(base)
    if payload:
        source.loadFromData(payload)
    logical_size = 18.0
    physical_size = max(18, int(math.ceil(logical_size * dpr)))
    canvas = QtGui.QPixmap(physical_size, physical_size)
    canvas.setDevicePixelRatio(dpr)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(canvas)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
    painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
    logical_rect = QtCore.QRectF(0.0, 0.0, logical_size, logical_size)
    if not source.isNull():
        painter.drawPixmap(logical_rect, source, QtCore.QRectF(source.rect()))
    else:
        painter.setPen(QtGui.QPen(QtGui.QColor(outline), 1))
        painter.setBrush(QtGui.QColor(surface))
        painter.drawEllipse(QtCore.QRectF(1, 1, 16, 16))
        painter.setFont(typography_font(TextRole.ICON_FALLBACK))
        painter.setPen(QtGui.QColor(ink))
        painter.drawText(logical_rect, Qt.AlignmentFlag.AlignCenter, base[:2])
    painter.end()
    icon = QtGui.QIcon(canvas)
    _WATCHLIST_ICON_CACHE[cache_key] = icon
    return icon


class MetricHoverReadout(QtWidgets.QFrame):
    """Compact tracker card kept outside the plot's paint path."""

    MAX_ROWS = 6

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("metricHoverReadout")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(5)

        self.stamp = QtWidgets.QLabel("—")
        self.stamp.setObjectName("metricHoverStamp")
        set_text_role(self.stamp, TextRole.UI_CAPTION)
        layout.addWidget(self.stamp)

        self.grid = QtWidgets.QGridLayout()
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(3)
        self.names: list[QtWidgets.QLabel] = []
        self.values: list[QtWidgets.QLabel] = []
        for row in range(self.MAX_ROWS):
            name = QtWidgets.QLabel()
            name.setObjectName("metricHoverName")
            set_text_role(name, TextRole.UI_LABEL)
            value = QtWidgets.QLabel()
            value.setObjectName("metricHoverValue")
            set_text_role(value, TextRole.MARKET_VALUE_EMPHASIZED)
            value.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            self.grid.addWidget(name, row, 0)
            self.grid.addWidget(value, row, 1)
            name.hide()
            value.hide()
            self.names.append(name)
            self.values.append(value)
        layout.addLayout(self.grid)

        self.note = QtWidgets.QLabel()
        self.note.setObjectName("metricHoverNote")
        set_text_role(self.note, TextRole.UI_BODY)
        self.note.setWordWrap(True)
        self.note.setMaximumWidth(290)
        self.note.hide()
        layout.addWidget(self.note)
        self.hide()

    def set_readout(
        self,
        stamp: str,
        rows: list[tuple[str, str]],
        note: str = "",
    ) -> None:
        self.stamp.setText(stamp)
        for index, (name_widget, value_widget) in enumerate(
            zip(self.names, self.values)
        ):
            if index < len(rows):
                name, value = rows[index]
                name_widget.setText(name)
                value_widget.setText(value)
                name_widget.show()
                value_widget.show()
            else:
                name_widget.hide()
                value_widget.hide()
        self.note.setText(note)
        self.note.setVisible(bool(note))
        self.adjustSize()
        self.show()
        self.raise_()


class MetricHistoryCanvas(QtWidgets.QWidget):
    """Market-history plot with a restrained crosshair and external tracker card."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("metricHistoryCanvas")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.points: list[tuple[float, float]] = []
        self.series: list[tuple[str, list[tuple[float, float]]]] = []
        self.series_maps: dict[str, dict[float, float]] = {}
        self.price_points: list[tuple[float, float]] = []
        self.price_enabled = False
        self.price_period_ms = 300_000
        self.price_status = ""
        self.percent = self.bars = self.ratio = False
        self.money = True
        self.hover_stamp: float | None = None
        self._hover_mouse_x: float | None = None
        self.details: dict[float, str] = {}
        self.time_range = None
        self.empty_text = "History not available"
        self.theme: dict[str, str] = {}
        self.setMinimumHeight(300)
        self.setMouseTracking(True)
        self.hover_readout = MetricHoverReadout(self)
        typography_controller().changed.connect(self.update)


    def set_named_series(
        self,
        series,
        *,
        percent=False,
        bars=False,
        ratio=False,
        money=True,
        details=None,
        time_range=None,
        empty="History not available",
    ):
        clean_series = []
        for name, points in series:
            clean = {}
            for stamp, value in points:
                try:
                    stamp, value = float(stamp), float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(stamp) and math.isfinite(value) and stamp > 0:
                    clean[stamp] = value
            clean_series.append((name, sorted(clean.items())[-4000:]))
        self.series = clean_series
        self.series_maps = {name: dict(points) for name, points in clean_series}
        self.points = sorted(
            {
                stamp: value
                for _name, points in clean_series
                for stamp, value in points
            }.items()
        )
        if self.hover_stamp not in {stamp for stamp, _value in self.points}:
            self.hover_stamp = None
            self.hover_readout.hide()
        self.percent, self.bars, self.ratio, self.money = (
            percent,
            bars,
            ratio,
            money,
        )
        self.details = details or {}
        self.time_range = time_range
        self.empty_text = empty
        self.update()

    def set_price_series(
        self,
        points,
        enabled=False,
        period="5m",
        unavailable=False,
    ):
        clean = {}
        for stamp, value in points:
            try:
                stamp, value = float(stamp), float(value)
            except (TypeError, ValueError):
                continue
            if (
                math.isfinite(stamp)
                and math.isfinite(value)
                and stamp > 0
                and value > 0
            ):
                clean[stamp] = value
        self.price_points = sorted(clean.items())
        self.price_enabled = enabled
        self.price_period_ms = {
            "5m": 300_000,
            "1h": 3600_000,
            "4h": 14400_000,
            "1d": 86400_000,
        }[period]
        self.price_status = "Price unavailable" if unavailable else "Price loading…"
        self._update_hover_readout()
        self.update()

    def _value(self, value):
        if self.ratio:
            return f"{value:.3f}"
        return (
            f"{value:+.4f}%"
            if self.percent
            else human_number(value, money=self.money)
        )

    def _price_axis_visible(self) -> bool:


        return bool(self.price_enabled and self.width() >= 178)

    def _bounds(self):
        price_axis_visible = self._price_axis_visible()
        width = max(1.0, float(self.width()))
        left = 90.0 if price_axis_visible else 14.0
        right = min(88.0, max(24.0, width - left - 40.0))
        return QtCore.QRectF(self.rect()).adjusted(
            left,
            48 if self.price_enabled else 34,
            -right,
            -30,
        )

    def apply_theme(self, theme: dict[str, str]) -> None:
        self.theme = theme
        self.update()

    def _colors(self) -> list[QtGui.QColor]:
        fallback = self.palette().color(QtGui.QPalette.ColorRole.Link).name()
        values = [
            self.theme.get("series_1", fallback),
            self.theme.get("series_2", "#B65C66"),
            self.theme.get("series_3", "#7A8FC7"),
            self.theme.get("series_4", "#C69B55"),
        ]
        if len(self.series) <= 1:
            return [QtGui.QColor(values[0])]
        return [QtGui.QColor(value) for value in values]

    def _nearest_price(self) -> tuple[float, float] | None:
        if self.hover_stamp is None or not self.price_points:
            return None
        return min(
            self.price_points,
            key=lambda point: abs(point[0] - self.hover_stamp),
        )

    def _update_hover_readout(self) -> None:
        if self.hover_stamp is None or not self.points:
            self.hover_readout.hide()
            return

        rows: list[tuple[str, str]] = []
        for name, _points in self.series:
            value = self.series_maps.get(name, {}).get(self.hover_stamp)
            rows.append((name, self._value(value) if value is not None else "—"))

        nearest_price = self._nearest_price()
        if nearest_price is not None:
            price_stamp, price_value = nearest_price
            if abs(price_stamp - self.hover_stamp) < self.price_period_ms:
                rows.append(("Price", f"{format_price(price_value)} USDT"))
            elif self.price_enabled:
                rows.append(("Price", "—"))
        elif self.price_enabled:
            rows.append(("Price", "—"))

        stamp_text = datetime.fromtimestamp(
            self.hover_stamp / 1000,
            timezone.utc,
        ).strftime("%d %b %Y · %H:%M UTC")
        note = str(self.details.get(self.hover_stamp, ""))
        self.hover_readout.set_readout(stamp_text, rows[:6], note)
        self._position_hover_readout(self._hover_mouse_x)

    def _position_hover_readout(self, mouse_x: float | None = None) -> None:
        if not self.hover_readout.isVisible():
            return
        self.hover_readout.adjustSize()
        margin = 12
        width = self.hover_readout.width()


        if mouse_x is not None and mouse_x > self.width() * 0.58:
            x = margin
        else:
            x = max(margin, self.width() - width - margin)
        self.hover_readout.move(x, margin)
        self.hover_readout.raise_()

    def paintEvent(self, event):
        del event
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        apply_text_render_hints(painter)
        text_color = self.palette().color(QtGui.QPalette.ColorRole.Text)
        muted = self.palette().color(QtGui.QPalette.ColorRole.Mid)
        painter.setPen(text_color)

        if not self.points:
            painter.setFont(typography_font(TextRole.UI_BODY))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                self.empty_text,
            )
            return

        bounds = self._bounds()
        if bounds.width() <= 0 or bounds.height() <= 0:
            return

        values = [
            value
            for _name, points in self.series
            for _stamp, value in points
        ]
        if not values:
            return
        low, high = min(values), max(values)
        if self.bars or self.percent:
            low, high = min(0.0, low), max(0.0, high)
        padding = max((high - low) * 0.08, abs(high) * 0.001, 1e-12)
        low, high = low - padding, high + padding
        start, end = self.time_range or (self.points[0][0], self.points[-1][0])

        def point(stamp, value):
            x = (
                bounds.center().x()
                if end == start
                else bounds.left()
                + (stamp - start) / (end - start) * bounds.width()
            )
            y = bounds.bottom() - (value - low) / (high - low) * bounds.height()
            return QtCore.QPointF(x, y)

        grid_color = QtGui.QColor(muted)
        grid_color.setAlpha(78)
        painter.setFont(typography_font(TextRole.CHART_AXIS))
        for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
            value = low + (high - low) * fraction
            y = point(start, value).y()
            painter.setPen(QtGui.QPen(grid_color, 1))
            painter.drawLine(
                QtCore.QPointF(bounds.left(), y),
                QtCore.QPointF(bounds.right(), y),
            )
            painter.setPen(muted)
            painter.drawText(
                device_pixel_rect(self, QtCore.QRectF(bounds.right() + 7, y - 9, 77, 18)),
                Qt.AlignmentFlag.AlignVCenter,
                self._value(value),
            )

        colors = self._colors()
        x_label = 12.0
        for index, (name, points) in enumerate(self.series):
            color = colors[index % len(colors)]
            value_text = self._value(points[-1][1]) if points else "—"
            painter.setPen(color)
            painter.setFont(typography_font(TextRole.UI_CAPTION))
            painter.drawText(QtCore.QPointF(x_label, 19), name)
            name_width = painter.fontMetrics().horizontalAdvance(name)
            value_x = x_label + name_width + 7.0
            painter.setFont(typography_font(TextRole.CHART_AXIS))
            painter.drawText(QtCore.QPointF(value_x, 19), value_text)
            x_label = (
                value_x
                + painter.fontMetrics().horizontalAdvance(value_text)
                + 24.0
            )

        price_color = QtGui.QColor(self.theme.get("reference_price", "#D4A94E"))
        price_points = (
            [
                (stamp, value)
                for stamp, value in self.price_points
                if start <= stamp <= end
            ]
            if self.price_enabled
            else []
        )
        price_point = None
        if self.price_enabled:
            painter.setPen(price_color)
            painter.setFont(typography_font(TextRole.CHART_AXIS))
            painter.drawText(
                QtCore.QPointF(12, 39),
                (
                    "PRICE  " + format_price(price_points[-1][1])
                    if price_points
                    else self.price_status.upper()
                ),
            )

        if price_points:
            price_low = min(value for _stamp, value in price_points)
            price_high = max(value for _stamp, value in price_points)
            price_pad = max(
                (price_high - price_low) * 0.08,
                abs(price_high) * 0.001,
                1e-12,
            )
            price_low, price_high = price_low - price_pad, price_high + price_pad

            def price_point(stamp, value):
                return QtCore.QPointF(
                    point(stamp, low).x(),
                    bounds.bottom()
                    - (value - price_low)
                    / (price_high - price_low)
                    * bounds.height(),
                )

            if self._price_axis_visible():
                painter.setFont(typography_font(TextRole.CHART_AXIS))
                for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
                    value = price_low + (price_high - price_low) * fraction
                    y = price_point(start, value).y()
                    painter.setPen(price_color)
                    painter.drawText(
                        device_pixel_rect(self, QtCore.QRectF(0, y - 9, max(20.0, bounds.left() - 8), 18)),
                        Qt.AlignmentFlag.AlignRight
                        | Qt.AlignmentFlag.AlignVCenter,
                        format_price(value),
                    )

        painter.save()
        painter.setClipRect(bounds.adjusted(-1, -1, 1, 1))
        zero = point(start, 0).y() if low <= 0 <= high else bounds.bottom()
        stamps = [stamp for stamp, _value in self.points]
        spacing = min(
            (b - a for a, b in zip(stamps, stamps[1:]) if b > a),
            default=max(1, end - start),
        )
        slot = bounds.width() * spacing / max(1, end - start)
        width = max(1.0, min(10.0, slot * 0.7)) / max(1, len(self.series))

        for index, (_name, points) in enumerate(self.series):
            color = colors[index % len(colors)]
            painter.setPen(QtGui.QPen(color, 1.45))
            if self.bars:
                for stamp, value in points:
                    p = point(stamp, value)
                    x = p.x() + (index - len(self.series) / 2) * width
                    painter.fillRect(
                        QtCore.QRectF(
                            x,
                            min(p.y(), zero),
                            width,
                            max(1.0, abs(zero - p.y())),
                        ),
                        color,
                    )
            elif points:
                path = QtGui.QPainterPath(point(*points[0]))
                for item in points[1:]:
                    path.lineTo(point(*item))
                painter.drawPath(path)

        if price_points and price_point is not None:
            painter.setPen(QtGui.QPen(price_color, 1.65))
            path = QtGui.QPainterPath(price_point(*price_points[0]))
            previous = price_points[0][0]
            for stamp, value in price_points[1:]:
                if stamp - previous > self.price_period_ms * 1.5:
                    path.moveTo(price_point(stamp, value))
                else:
                    path.lineTo(price_point(stamp, value))
                previous = stamp
            painter.drawPath(path)

        if self.hover_stamp is not None:
            x = point(self.hover_stamp, low).x()
            crosshair = QtGui.QColor(muted)
            crosshair.setAlpha(165)
            painter.setPen(
                QtGui.QPen(crosshair, 1, Qt.PenStyle.DashLine)
            )
            painter.drawLine(
                QtCore.QPointF(x, bounds.top()),
                QtCore.QPointF(x, bounds.bottom()),
            )
            for index, (name, _points) in enumerate(self.series):
                value = self.series_maps.get(name, {}).get(self.hover_stamp)
                if value is None:
                    continue
                color = colors[index % len(colors)]
                painter.setPen(QtGui.QPen(color, 1))
                painter.setBrush(color)
                painter.drawEllipse(point(self.hover_stamp, value), 3.5, 3.5)

            nearest_price = self._nearest_price()
            if (
                nearest_price is not None
                and price_point is not None
                and abs(nearest_price[0] - self.hover_stamp) < self.price_period_ms
            ):
                painter.setPen(QtGui.QPen(price_color, 1))
                painter.setBrush(price_color)
                painter.drawEllipse(price_point(*nearest_price), 3.5, 3.5)

        painter.restore()

        painter.setPen(muted)
        painter.setFont(typography_font(TextRole.CHART_AXIS))
        for stamp, alignment in (
            (start, Qt.AlignmentFlag.AlignLeft),
            (end, Qt.AlignmentFlag.AlignRight),
        ):
            label = datetime.fromtimestamp(
                stamp / 1000,
                timezone.utc,
            ).strftime("%d %b %H:%M")
            painter.drawText(
                device_pixel_rect(
                    self,
                    QtCore.QRectF(
                        bounds.left(),
                        self.height() - 22,
                        bounds.width(),
                        18,
                    ),
                ),
                alignment,
                label,
            )

    def mouseMoveEvent(self, event):
        bounds = self._bounds()
        self._hover_mouse_x = float(event.position().x())
        if self.points and bounds.contains(event.position()):
            start, end = self.time_range or (
                self.points[0][0],
                self.points[-1][0],
            )
            target = start + (end - start) * (
                event.position().x() - bounds.left()
            ) / max(1, bounds.width())
            self.hover_stamp = min(
                self.points,
                key=lambda point: abs(point[0] - target),
            )[0]
            self._update_hover_readout()
        else:
            self.hover_stamp = None
            self.hover_readout.hide()
        self.update()

    def leaveEvent(self, event):
        self.hover_stamp = None
        self._hover_mouse_x = None
        self.hover_readout.hide()
        self.update()
        super().leaveEvent(event)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._position_hover_readout(self._hover_mouse_x)


class MetricDetailDialog(QtWidgets.QDialog):
    """Shared, switchable history shell for the five primary market metrics."""

    def __init__(self, card):
        super().__init__(
            card.window(),
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint,
        )
        self.card = card
        self.stats_owner = getattr(card, "metric_owner", None)
        self.metric_key = str(getattr(card, "metric_key", ""))
        self.period = "5m"
        self.generation = 0
        self.cache: dict[tuple, tuple[float, dict[str, Any]]] = {}
        self.tasks = set()
        self.series_options = []
        self.series_format = {}
        self.setObjectName("metricDetailDialog")


        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumSize(680, 440)
        self.resize(760, 500)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(9)

        header = QtWidgets.QFrame()
        header.setObjectName("metricDialogHeader")
        header_row = QtWidgets.QHBoxLayout(header)
        header_row.setContentsMargins(10, 7, 6, 7)
        header_row.setSpacing(8)

        title_column = QtWidgets.QVBoxLayout()
        title_column.setContentsMargins(0, 0, 0, 0)
        title_column.setSpacing(1)
        self.heading = QtWidgets.QLabel()
        self.heading.setObjectName("metricDialogHeading")
        self.subheading = QtWidgets.QLabel()
        self.subheading.setObjectName("metricDialogSubheading")
        title_column.addWidget(self.heading)
        title_column.addWidget(self.subheading)
        header_row.addLayout(title_column, 1)

        close = QtWidgets.QPushButton("×")
        close.setObjectName("framelessCloseButton")
        set_text_role(close, TextRole.UI_GLYPH)
        close.setFixedSize(28, 28)
        close.setAccessibleName("Close · Escape")
        close.setAutoDefault(False)
        close.clicked.connect(self.reject)
        header_row.addWidget(close)
        root.addWidget(header)

        self.metric_tabs = QtWidgets.QFrame()
        self.metric_tabs.setObjectName("metricDialogTabs")
        tabs_row = QtWidgets.QHBoxLayout(self.metric_tabs)
        tabs_row.setContentsMargins(0, 0, 0, 0)
        tabs_row.setSpacing(5)
        self.metric_group = QtWidgets.QButtonGroup(self)
        self.metric_group.setExclusive(True)
        self.metric_buttons: dict[str, QtWidgets.QPushButton] = {}
        for key, label in METRIC_DETAIL_ORDER:
            button = QtWidgets.QPushButton(label)
            button.setObjectName("metricDialogTab")
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(
                lambda _checked=False, value=key: self._switch_metric(value)
            )
            self.metric_group.addButton(button)
            self.metric_buttons[key] = button
            tabs_row.addWidget(button, 1)
        root.addWidget(self.metric_tabs)

        controls = QtWidgets.QFrame()
        controls.setObjectName("metricDialogControls")
        toolbar = QtWidgets.QHBoxLayout(controls)
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(5)

        self.series_label = QtWidgets.QLabel("SERIES")
        self.series_label.setObjectName("metricControlLabel")
        self.series_selector = QtWidgets.QComboBox()
        self.series_selector.setObjectName("metricSeriesSelector")
        self.series_selector.setAccessibleName("Metric series")
        self.series_selector.setMinimumWidth(190)
        self.series_selector.currentIndexChanged.connect(self._select_series)
        toolbar.addWidget(self.series_label)
        toolbar.addWidget(self.series_selector, 1)

        toolbar.addStretch(1)
        self.period_group = QtWidgets.QButtonGroup(self)
        self.period_group.setExclusive(True)
        self.period_buttons: dict[str, QtWidgets.QPushButton] = {}
        for period, title in (
            ("5m", "5m"),
            ("1h", "1h"),
            ("4h", "4h"),
            ("1d", "1D"),
        ):
            button = QtWidgets.QPushButton(title)
            button.setObjectName("metricPeriodButton")
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setFixedWidth(42)
            button.setChecked(period == self.period)
            button.clicked.connect(
                lambda _checked=False, value=period: self._change_period(value)
            )
            self.period_group.addButton(button)
            self.period_buttons[period] = button
            toolbar.addWidget(button)

        self.price_toggle = QtWidgets.QPushButton("PRICE")
        self.price_toggle.setObjectName("metricPriceToggle")
        self.price_toggle.setCheckable(True)
        self.price_toggle.setAutoDefault(False)
        self.price_toggle.setAccessibleName(
            "Overlay price with an independent scale"
        )
        self.price_toggle.setFixedWidth(62)
        self.price_toggle.toggled.connect(lambda _checked: self._load())
        toolbar.addWidget(self.price_toggle)
        root.addWidget(controls)

        self.chart = MetricHistoryCanvas()
        metric_owner = getattr(card, "metric_owner", None)
        if metric_owner is not None:
            self.chart.apply_theme(getattr(metric_owner, "theme", {}))
        root.addWidget(self.chart, 1)

        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.setInterval(60_000)
        self.refresh_timer.timeout.connect(lambda: self._load(force=True))
        self._sync_header()

    def apply_theme(self, theme: dict[str, str]) -> None:
        self.chart.apply_theme(theme)

    def _metric_label(self) -> str:
        return dict(METRIC_DETAIL_ORDER).get(
            self.metric_key,
            self.card.title.text(),
        )

    def _sync_header(self) -> None:
        symbol = perpetual_display_symbol(self.card.history_symbol)
        self.heading.setText(f"{symbol} · MARKET DATA")
        self.subheading.setText(self._metric_label())
        self.setWindowTitle(f"{symbol} · {self._metric_label()}")
        switchable = (
            self.stats_owner is not None
            and self.metric_key in METRIC_DETAIL_KEYS
        )
        self.metric_tabs.setVisible(switchable)
        for key, button in self.metric_buttons.items():
            blocker = QtCore.QSignalBlocker(button)
            button.setChecked(switchable and key == self.metric_key)
            del blocker

    def set_card(self, card, *, load: bool = True) -> None:
        if card is None:
            return
        changed = card is not self.card
        self.card = card
        self.stats_owner = getattr(card, "metric_owner", self.stats_owner)
        self.metric_key = str(getattr(card, "metric_key", self.metric_key))
        self.generation += 1
        if changed:
            with QtCore.QSignalBlocker(self.series_selector):
                self.series_selector.clear()
            self.series_options = []
            self.chart.hover_stamp = None
            self.chart.hover_readout.hide()
            self.chart.set_named_series([], empty="Loading…")
        self._sync_header()
        if load and self.isVisible():
            self._load()

    def _switch_metric(self, metric_key: str) -> None:
        if (
            metric_key == self.metric_key
            or self.stats_owner is None
            or metric_key not in METRIC_DETAIL_KEYS
        ):
            return
        target = self.stats_owner.cards.get(metric_key)
        if target is not None:
            self.set_card(target, load=True)

    def set_histories(self, options, *, percent=False, bars=False, ratio=False):
        self._apply(
            {
                "options": [
                    {"name": name, "series": [(name, points)]}
                    for name, points, _caption in options
                ],
                "percent": percent,
                "bars": bars,
                "ratio": ratio,
            }
        )

    def _apply(self, payload):
        self.chart.set_price_series(
            payload.get("price_points", []),
            self.price_toggle.isChecked(),
            self.period,
            payload.get(
                "price_unavailable",
                not bool(payload.get("price_points")),
            ),
        )
        self.series_options = payload.get("options", [])
        self.series_format = {
            key: payload[key]
            for key in ("percent", "bars", "ratio", "money", "time_range")
            if key in payload
        }
        names = [option["name"] for option in self.series_options]
        selected = self.series_selector.currentText()
        with QtCore.QSignalBlocker(self.series_selector):
            self.series_selector.clear()
            self.series_selector.addItems(names)
            if selected in names:
                self.series_selector.setCurrentText(selected)
        multiple = len(names) > 1
        self.series_label.setVisible(multiple)
        self.series_selector.setVisible(multiple)
        self._select_series(max(0, self.series_selector.currentIndex()))

    def _select_series(self, index):
        if 0 <= index < len(self.series_options):
            option = self.series_options[index]
            self.chart.set_named_series(
                option.get("series", []),
                details=option.get("details"),
                empty=option.get("empty", "No samples in this period"),
                **self.series_format,
            )

    def _change_period(self, period):
        if period != self.period:
            self.period = period
            self._load()

    def _load(self, force=False):
        loader = self.card.history_loader
        if loader is None:
            return
        self.generation += 1
        generation = self.generation
        requested_metric = self.metric_key
        symbol = self.card.history_symbol
        key = (
            requested_metric,
            symbol,
            self.period,
            self.price_toggle.isChecked(),
        )
        cached = self.cache.get(key)
        if not force and cached and time.monotonic() - cached[0] < 30:
            self._apply(cached[1])
            return

        self.chart.set_named_series([], empty="Loading…")
        self.chart.set_price_series(
            [],
            self.price_toggle.isChecked(),
            self.period,
        )
        period = self.period
        include_price = self.price_toggle.isChecked()

        def finish(payload):
            self.tasks.discard(task)
            if (
                generation != self.generation
                or requested_metric != self.metric_key
                or symbol != self.card.history_symbol
                or not self.isVisible()
            ):
                return
            if len(self.cache) >= 16:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = time.monotonic(), payload
            self._apply(payload)

        def failed(_message):
            self.tasks.discard(task)
            if (
                generation == self.generation
                and requested_metric == self.metric_key
                and symbol == self.card.history_symbol
                and self.isVisible()
            ):
                self.chart.set_named_series(
                    [],
                    empty="History unavailable · change period or reopen to retry",
                )

        task = launch_task(
            lambda: loader(symbol, period, include_price),
            finish,
            failed,
        )
        self.tasks.add(task)

    def _fit_content(self):
        owner = self.parentWidget()
        screen = owner.screen() if owner is not None else self.screen()
        available = screen.availableGeometry().adjusted(12, 12, -12, -12)
        width = min(780, available.width())
        height = min(520, available.height())
        self.resize(max(680, width), max(440, height))

    def open(self):
        owner = self.parentWidget()
        screen = owner.screen() if owner is not None else self.screen()
        available = screen.availableGeometry().adjusted(12, 12, -12, -12)
        self._fit_content()
        center = (
            owner.frameGeometry().intersected(available).center()
            if owner is not None
            else available.center()
        )
        self.move(
            max(
                available.left(),
                min(
                    center.x() - self.width() // 2,
                    available.right() - self.width() + 1,
                ),
            ),
            max(
                available.top(),
                min(
                    center.y() - self.height() // 2,
                    available.bottom() - self.height() + 1,
                ),
            ),
        )
        self._sync_header()


        super().show()
        self.raise_()
        self.activateWindow()
        self._load()
        self.refresh_timer.start()

    def done(self, result):
        self.generation += 1
        self.refresh_timer.stop()
        self.chart.hover_readout.hide()
        super().done(result)


class TickerPriceLabel(QtWidgets.QLabel):
    """Live top-ticker price label; semantic typography owns its font."""

    pass


class MetricCard(QtWidgets.QFrame):
    width_changed = Signal()

    def __init__(
        self,
        title: str,
        value: str = "—",
        parent: QtWidgets.QWidget | None = None,
        compact: bool = False,
        identity: bool = False,
    ):
        super().__init__(parent)
        self.detail_title = title
        self.history = []
        self.history_options = []
        self.history_loader = None
        self.history_symbol = DEFAULT_SYMBOL
        self.history_caption = ""
        self.history_percent = False
        self.history_bars = False
        self.history_ratio = False
        self.detail_popup = None
        self.identity = bool(identity and compact)
        self.compact = bool(compact)
        self._reported_metric_width = None
        self.setObjectName(
            "topMarketIdentity"
            if self.identity
            else "topMetricChip" if compact else "metricCard"
        )
        if compact:
            self.setProperty("instrumentClickable", not self.identity)
        layout = (QtWidgets.QHBoxLayout if self.identity else QtWidgets.QVBoxLayout)(self)
        # The instrument row supplies the other 12 px of the 96 px left gap.
        layout.setContentsMargins(84 if self.identity else 12 if compact else 5,
                                  1 if compact else 5,
                                  96 if self.identity else 12 if compact else 5,
                                  1 if compact else 5)
        layout.setSpacing(32 if self.identity else 1 if compact else 2)

        self.title = QtWidgets.QLabel(title.upper())
        self.title.setObjectName(
            "topTickerSymbol"
            if self.identity
            else "topMetricTitle" if compact else "metricTitle"
        )
        self.value = (
            TickerPriceLabel(value)
            if self.identity
            else QtWidgets.QLabel(value)
        )
        self.value.setObjectName(
            "topTickerLast"
            if self.identity
            else "topMetricValue" if compact else "metricValue"
        )
        set_text_role(
            self.title,
            TextRole.TOP_TICKER_SYMBOL if self.identity else TextRole.UI_CAPTION if compact else TextRole.UI_LABEL,
        )


        set_text_role(self.value, TextRole.ORDERBOOK_CENTER_PRICE if self.identity else TextRole.TOP_MARKET_VALUE if compact else TextRole.MARKET_VALUE)
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.value.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        if compact:
            self.setMinimumWidth(0)
            self.setSizePolicy(
                (
                    QtWidgets.QSizePolicy.Policy.Fixed
                    if self.identity
                    else QtWidgets.QSizePolicy.Policy.Ignored
                ),
                QtWidgets.QSizePolicy.Policy.Fixed,
            )
            self.setFixedHeight(COMPACT_METRIC_HEIGHT)
            self.value.setAlignment(
                (Qt.AlignmentFlag.AlignLeft)
                | Qt.AlignmentFlag.AlignVCenter
            )
            self.title.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            if not self.identity:


                self.title.setWordWrap(False)
            self.title.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Minimum if self.identity else QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Preferred,
            )
            self.value.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Preferred,
            )


        for widget in (self, self.title, self.value):
            widget.setProperty("informationalToolTip", True)
        self.setToolTipDuration(8_000)
        layout.addWidget(self.title)
        layout.addWidget(self.value)
        if compact:
            alignment = Qt.AlignmentFlag.AlignVCenter
            if self.identity:
                alignment |= Qt.AlignmentFlag.AlignLeft
            layout.setAlignment(self.title, alignment)
            layout.setAlignment(self.value, alignment)
        if self.identity:
            QtCore.QTimer.singleShot(0, self._sync_identity_width)
        elif compact:
            self.title.installEventFilter(self)
            self.value.installEventFilter(self)
            self._sync_metric_width()

    def metric_width(self) -> int:
        margins = self.layout().contentsMargins()
        content = max(self.title.fontMetrics().horizontalAdvance(self.title.text()),
                      self.value.fontMetrics().horizontalAdvance(self.value.text()))
        return max(COMPACT_EQUAL_METRIC_WIDTH,
                   content + margins.left() + margins.right() + 2 * self.frameWidth())

    def _sync_metric_width(self) -> None:
        if not self.compact or self.identity:
            return
        width = self.metric_width()
        if width != self._reported_metric_width:
            self._reported_metric_width = width
            self.updateGeometry()
            self.width_changed.emit()

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() in (QtCore.QEvent.Type.FontChange, QtCore.QEvent.Type.StyleChange):
            self._sync_metric_width()
        return super().eventFilter(watched, event)

    def identity_width(self, *, horizontal_margins: tuple[int, int] | None = None) -> int:
        """Measure ticker content with the requested padding, independent of its current width."""
        margins = self.layout().contentsMargins()
        left, right = horizontal_margins if horizontal_margins is not None else (margins.left(), margins.right())
        return max(COMPACT_IDENTITY_WIDTH,
                   max(self.title.sizeHint().width(), self.title.fontMetrics().horizontalAdvance(self.title.text()))
                   + max(self.value.sizeHint().width(), self.value.fontMetrics().horizontalAdvance(self.value.text()))
                   + self.layout().spacing() + left + right)

    def set_identity_horizontal_margins(self, left: int, right: int) -> None:
        if not self.identity:
            return
        layout = self.layout()
        margins = layout.contentsMargins()
        if (margins.left(), margins.right()) == (left, right):
            return
        layout.setContentsMargins(left, margins.top(), right, margins.bottom())
        self._sync_identity_width()

    def _sync_identity_width(self) -> None:
        if not self.identity:
            return


        if self.compact:
            width = self.identity_width()
            if self.property("instrumentFlexOwned"):
                if self.width() != width or self.minimumWidth() != width:
                    self.setFixedWidth(width)
                    self.updateGeometry()
                    self.width_changed.emit()
            else:
                self.setMinimumWidth(width)
                self.setMaximumWidth(max(width, COMPACT_IDENTITY_WIDTH * 2))
            return
        margins = self.layout().contentsMargins()
        padding = margins.left() + margins.right() + 2
        content_width = max(
            self.title.fontMetrics().horizontalAdvance(self.title.text()),
            self.value.fontMetrics().horizontalAdvance(self.value.text()),
        )
        target = max(self.height(), content_width + padding)
        if self.width() != target:
            self.setFixedWidth(target)
            self.updateGeometry()

    def set_value(self, text: str, color: str | None = None) -> None:
        if self.value.text() != text:
            self.value.setText(text)
        stylesheet = f"color: {color};" if color else ""
        if self.value.styleSheet() != stylesheet:
            self.value.setStyleSheet(stylesheet)
        self._sync_identity_width()
        self._sync_metric_width()

    def set_detail(self, _title: str, rows: list[tuple[str, str]]) -> None:
        rows = [(label, value) for label, value in rows if label or value]
        self.detail_title, self.detail_rows = _title, rows
        body = "".join(
            f"<tr><td style='padding-right:18px'>{html.escape(str(label))}</td>"
            f"<td align='right'>{html.escape(str(value))}</td></tr>"
            for label, value in rows
        )
        tooltip = f"<table cellspacing='0' cellpadding='0'>{body}</table>"


        actionable = (not self.identity) or bool(COMPACT_SECONDARY_METRICS)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor
            if actionable
            else Qt.CursorShape.ArrowCursor
        )
        for widget in (self, self.title, self.value):
            widget.setToolTip(tooltip)
            widget.setToolTipDuration(8_000)
        self._refresh_popup()

    def set_history(self, points, caption, *, percent=False, bars=False, ratio=False):
        self.history_options = []
        self.history, self.history_caption = points, caption
        self.history_percent, self.history_bars, self.history_ratio = percent, bars, ratio
        self._refresh_popup()

    def set_histories(self, options, *, ratio=False):
        self.history_options = options
        self.history_caption = options[0][2] if options else ""
        self.history_ratio, self.history_percent, self.history_bars = ratio, False, False
        self._refresh_popup()

    def _refresh_popup(self):
        if self.detail_popup is not None and self.history_loader is None:
            self.detail_popup.set_histories(self.history_options or [(self.detail_title, self.history, self.history_caption)],
                                           percent=self.history_percent, bars=self.history_bars, ratio=self.history_ratio)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            QtWidgets.QToolTip.hideText()
            owner = getattr(self, "metric_owner", None)
            metric_key = str(getattr(self, "metric_key", ""))
            if (
                self.identity
                and self.compact
                and owner is not None
                and COMPACT_SECONDARY_METRICS
            ):
                owner.open_compact_secondary_metrics(
                    self.mapToGlobal(QtCore.QPoint(0, self.height()))
                )
                event.accept()
                return
            if not self.identity:
                if owner is not None and metric_key in METRIC_DETAIL_KEYS:
                    owner.open_metric_detail(metric_key)
                else:
                    if self.detail_popup is None:
                        self.detail_popup = MetricDetailDialog(self)
                    self._refresh_popup()
                    self.detail_popup.open()
                event.accept()
                return
        super().mousePressEvent(event)

    def clear_detail(self) -> None:
        self.history_options = []
        self.history = []
        self.history_caption = ""
        if self.detail_popup is not None:
            self.detail_popup.reject()
            self.detail_popup.cache.clear()
        self.unsetCursor()
        for widget in (self, self.title, self.value):
            widget.setToolTip("")

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        QtWidgets.QToolTip.hideText()
        super().leaveEvent(event)


class MarketStatsWidget(QtWidgets.QWidget):
    timeframe_selected = Signal(str)

    def __init__(
        self,
        theme: dict[str, str],
        parent: QtWidgets.QWidget | None = None,
        compact: bool = False,
    ):
        super().__init__(parent)
        self.setObjectName("topMarketStats" if compact else "marketStats")
        self.theme = theme
        self.symbol = DEFAULT_SYMBOL
        self.mark = 0.0
        self.last_ticker: dict[str, Any] = {}
        self.last_mark_payload: dict[str, Any] = {}
        self.last_interest_payload: dict[str, Any] = {}
        self.last_interest_reference = 0.0
        self.last_taker_buy_pct = float("nan")
        self.compact = compact
        self.cards = {
            name: MetricCard(
                title,
                compact=compact,
                identity=compact and name == "last",
            )
            for name, title in (
                ("last", perpetual_display_symbol(self.symbol)),
                ("volume", "24H VOLUME"),
                ("taker", "TAKER VOLUME"),
                ("oi", "OPEN INTEREST"),
                ("long_short", "LONG/SHORT RATIO"),
                ("funding", "FUNDING RATE"),
            )
        }
        self.metric_detail_popup: MetricDetailDialog | None = None
        for name, card in self.cards.items():
            card.metric_key = name
            card.metric_owner = self
        if compact:
            compact_titles = {
                "volume": "24H VOLUME",
                "taker": "Taker volume",
                "oi": "OPEN INTEREST",
                "long_short": "LONG/SHORT",
                "funding": "FUNDING RATE",
            }
            for name, title in compact_titles.items():
                self.cards[name].title.setText(title)

            outer = QtWidgets.QHBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.setSpacing(COMPACT_METRIC_GAP)
            outer.setAlignment(Qt.AlignmentFlag.AlignVCenter)

            self.cards["last"].setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Fixed,
            )
            self.cards["last"].setMinimumWidth(COMPACT_IDENTITY_WIDTH)
            self.cards["last"].setMaximumWidth(16777215)
            self.cards["last"].setProperty("instrumentRole", "identity")
            outer.addWidget(self.cards["last"])


            self.timeframe_selector = TimeframeStrip(self)
            self.timeframe_selector.setObjectName("topTimeframeStrip")
            self.timeframe_selector.setFixedHeight(COMPACT_METRIC_HEIGHT)
            self.timeframe_selector.setAccessibleName("Chart timeframe")
            self.timeframe_selector.setItems(DEFAULT_MARKET_BAR_TIMEFRAMES)
            self.timeframe_selector.activated.connect(
                lambda _index: self.timeframe_selected.emit(
                    str(self.timeframe_selector.currentData() or "")
                )
            )


            for name in COMPACT_PRIMARY_METRICS:
                self.cards[name].setProperty("instrumentGroupStart", name == "volume")
                self.cards[name].setFixedWidth(COMPACT_EQUAL_METRIC_WIDTH)
                self.cards[name].setSizePolicy(
                    QtWidgets.QSizePolicy.Policy.Fixed,
                    QtWidgets.QSizePolicy.Policy.Fixed,
                )
                self.cards[name].setProperty("instrumentRole", "primary")
                outer.addWidget(self.cards[name])
            for name in COMPACT_SECONDARY_METRICS:
                self.cards[name].setSizePolicy(
                    QtWidgets.QSizePolicy.Policy.Fixed,
                    QtWidgets.QSizePolicy.Policy.Fixed,
                )
                self.cards[name].setProperty("instrumentRole", "secondary")
                outer.addWidget(self.cards[name])
                self.cards[name].hide()

            self.setMinimumWidth(0)
            self.setFixedHeight(COMPACT_METRIC_HEIGHT)
            self.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Fixed,
                QtWidgets.QSizePolicy.Policy.Fixed,
            )
        else:
            grid = QtWidgets.QGridLayout(self)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setSpacing(5)
            for index, card in enumerate(self.cards.values()):
                grid.addWidget(card, index // 4, index % 4)
        self.reset()

    def set_symbol(self, symbol: str) -> None:
        new_symbol = str(symbol).upper().strip().removesuffix(".P") or DEFAULT_SYMBOL
        if (
            self.metric_detail_popup is not None
            and new_symbol != self.symbol
        ):
            self.metric_detail_popup.reject()
            self.metric_detail_popup.cache.clear()
        self.symbol = new_symbol
        display = perpetual_display_symbol(self.symbol)
        self.cards["last"].title.setText(display)
        self.cards["last"].setAccessibleName(f"{display} last price")
        for card in self.cards.values():
            if card.history_symbol != self.symbol and card.detail_popup is not None:
                card.detail_popup.reject()
                card.detail_popup.cache.clear()
            card.history_symbol = self.symbol
        self.cards["last"].show()
        self.cards["last"]._sync_identity_width()

    def set_interval(self, interval: str) -> None:
        selector = getattr(self, "timeframe_selector", None)
        if selector is None:
            return
        if str(interval) == selector.currentData():
            return
        selector.setCurrentData(str(interval))

    def set_timeframes(self, intervals: object, current: str | None = None) -> None:
        selector = getattr(self, "timeframe_selector", None)
        if selector is not None:
            selector.setItems(normalized_market_bar_timeframes(intervals), current)

    def open_metric_detail(self, metric_key: str) -> None:
        if metric_key not in METRIC_DETAIL_KEYS:
            return
        card = self.cards.get(metric_key)
        if card is None:
            return
        if self.metric_detail_popup is None:
            self.metric_detail_popup = MetricDetailDialog(card)
        else:
            self.metric_detail_popup.set_card(card, load=False)
        self.metric_detail_popup.open()

    def open_compact_secondary_metrics(self, global_position: QtCore.QPoint) -> None:
        """Open any metrics intentionally omitted from the permanent compact strip."""
        if not self.compact or not COMPACT_SECONDARY_METRICS:
            return
        labels = {"taker": "Taker volume", "long_short": "Long / short"}
        menu = QtWidgets.QMenu(self)
        for key in COMPACT_SECONDARY_METRICS:
            card = self.cards[key]
            value = card.value.text().strip() or "—"
            action = menu.addAction(f"{labels.get(key, key.title())}   {value}")
            action.triggered.connect(
                lambda _checked=False, metric=key: self.open_metric_detail(metric)
            )
        menu.exec(global_position)

    def sizeHint(self) -> QtCore.QSize:
        hint = super().sizeHint()
        if self.compact:


            identity_attached = self.cards["last"].parentWidget() is self
            metric_count = len(COMPACT_PRIMARY_METRICS)
            total_width = COMPACT_EQUAL_METRIC_WIDTH * metric_count
            if metric_count > 1:
                total_width += (metric_count - 1) * COMPACT_METRIC_GAP
            if identity_attached:
                total_width += COMPACT_IDENTITY_WIDTH + COMPACT_METRIC_GAP
            return QtCore.QSize(total_width, COMPACT_METRIC_HEIGHT)
        return hint

    def reset(self) -> None:
        self.mark = 0.0
        self.last_ticker.clear()
        self.last_mark_payload.clear()
        self.last_interest_payload.clear()
        self.last_interest_reference = 0.0
        self.last_taker_buy_pct = float("nan")
        self.long_short_series = {}
        for card in self.cards.values():
            card.set_value("—")
            card.clear_detail()
        self.cards["last"].set_detail(
            "MARKET PRICE",
            [("Status", "Waiting for market data…")],
        )
        for name in ("volume", "taker", "oi", "long_short", "funding"):
            self.cards[name].set_detail(self.cards[name].detail_title, [("Status", "Waiting for market data…")])

    def update_long_short(self, rows, top_accounts=None, top_positions=None, requires_key=False):
        groups = (
            ("Top trader accounts", top_accounts or [], "Top 20% by margin balance · each account counted once"),
            ("Top trader positions", top_positions or [], "Top 20% by margin balance · share of open positions"),
            ("All accounts", rows, "Accounts with net long/short positions"),
        )
        details, histories = [], []
        cache = getattr(self, "long_short_series", {})
        for name, incoming, meaning in groups:
            valid = {}
            for row in incoming:
                if not isinstance(row, dict):
                    continue
                stamp = safe_float(row.get("timestamp"))
                ratio = safe_float(row.get("longShortRatio"), -1)
                if math.isfinite(stamp) and math.isfinite(ratio) and stamp > 0 and ratio >= 0:
                    valid[stamp] = dict(row)
            if valid:
                cache[name] = [valid[stamp] for stamp in sorted(valid)][-30:]
            samples = cache.get(name, [])
            histories.append((name, [(r["timestamp"], r["longShortRatio"]) for r in samples],
                              meaning + " · 5-minute samples"))
            details.append((name.upper(), ""))
            if not samples:
                details.append(("Status", "Add Binance API key in Trading settings" if name != "All accounts" and requires_key
                                else "Not available for this pair"))
                continue
            row = samples[-1]
            def share(key):
                value = safe_float(row.get(key), -1)
                return f"{value * 100:.1f}%" if math.isfinite(value) and 0 <= value <= 1 else "—"


            unit = "positions" if name == "Top trader positions" else "accounts"
            stamp = safe_float(row["timestamp"])
            details.extend([
                ("L/S ratio", f"{safe_float(row['longShortRatio']):.3f}"),
                (f"Long {unit}", share("longAccount")),
                (f"Short {unit}", share("shortAccount")),
                ("Updated", datetime.fromtimestamp(stamp / 1000, timezone.utc).strftime("%d %b %H:%M UTC")),
            ])
            if not valid or time.time() * 1000 - stamp > 900_000:
                details.append(("Status", "Last available sample"))
        self.long_short_series = cache
        card = self.cards["long_short"]
        card.title.setText("LONG/SHORT" if self.compact else "LONG/SHORT RATIO")


        samples = cache.get("All accounts", [])
        ratio = safe_float(samples[-1].get("longShortRatio"), -1) if samples else -1
        ratio_text = f"{ratio:.2f}" if math.isfinite(ratio) and ratio >= 0 else "—"
        shown = ratio_text
        if self.compact and samples:
            long_share = safe_float(samples[-1].get("longAccount"), float("nan"))
            short_share = safe_float(samples[-1].get("shortAccount"), float("nan"))
            if not (0 <= long_share <= 1 and 0 <= short_share <= 1
                    and math.isclose(long_share + short_share, 1, abs_tol=0.001)):
                long_share = ratio / (1 + ratio) if ratio >= 0 else float("nan")
            if 0 <= long_share <= 1:
                long_pct = round(long_share * 100, 1)
                shown = f"{long_pct:.1f}% / {100 - long_pct:.1f}%"
            else:
                shown = "—"
        card.set_value(shown, None)
        card.set_detail(f"{perpetual_display_symbol(self.symbol)} · LONG / SHORT", details)
        card.set_histories(histories, ratio=True)
        if self.compact:
            self._sync_last_detail()

    def bind_metric_history(self, rest):
        for name, card in self.cards.items():
            if name != "last":
                card.history_loader = lambda symbol, period, include_price=False, metric=name: rest.metric_history(symbol, metric, period, include_price)

    def invalidate_metric_histories(self, *_args):
        if self.metric_detail_popup is not None:
            self.metric_detail_popup.cache.clear()
            self.metric_detail_popup.generation += 1
            if self.metric_detail_popup.isVisible():
                self.metric_detail_popup._load(force=True)
        for card in self.cards.values():
            if card.detail_popup is not None:
                card.detail_popup.cache.clear()
                card.detail_popup.generation += 1
                if card.detail_popup.isVisible():
                    card.detail_popup._load(force=True)

    def update_taker_volume(self, rows):
        now = time.time() * 1000
        rows = [
            r
            for r in (rows or [])
            if safe_float(r.get("timestamp")) + 300_000 <= now
        ]
        card = self.cards["taker"]
        if not rows:
            self.last_taker_buy_pct = float("nan")
            card.set_value("—")
            card.set_detail(
                f"{perpetual_display_symbol(self.symbol)} · TAKER VOLUME",
                [("Status", "No completed 5-minute taker sample available")],
            )
            if self.compact:
                self._sync_last_detail()
            return
        latest = max(rows, key=lambda r: safe_float(r.get("timestamp")))
        buy, sell = safe_float(latest.get("buyVol")), safe_float(latest.get("sellVol"))
        total = buy + sell
        buy_pct = (buy / total * 100.0) if total > 0 else float("nan")
        sell_pct = (sell / total * 100.0) if total > 0 else float("nan")
        self.last_taker_buy_pct = buy_pct


        if math.isfinite(buy_pct) and buy_pct > 65.0:
            taker_color = self.theme.get("metric_taker_buy", self.theme["green"])
        elif math.isfinite(buy_pct) and buy_pct < 35.0:
            taker_color = self.theme.get("metric_taker_sell", self.theme["red"])
        else:
            taker_color = self.theme.get("metric_taker_neutral", self.theme["text"])


        card.set_value(
            f"{buy_pct:.1f}%" if math.isfinite(buy_pct) else "—",
            taker_color if math.isfinite(buy_pct) else None,
        )
        sample_end_ms = safe_float(latest.get("timestamp")) + 300_000
        sample_end = (
            datetime.fromtimestamp(sample_end_ms / 1000.0, timezone.utc).strftime(
                "%d %b %H:%M UTC"
            )
            if sample_end_ms > 300_000
            else "—"
        )
        card.set_detail(
            f"{perpetual_display_symbol(self.symbol)} · TAKER VOLUME",
            [
                ("Taker buy share", f"{buy_pct:.1f}%" if math.isfinite(buy_pct) else "—"),
                ("Taker sell share", f"{sell_pct:.1f}%" if math.isfinite(sell_pct) else "—"),
                ("Buy volume", human_number(buy)),
                ("Sell volume", human_number(sell)),
                ("Window", "Completed 5-minute sample"),
                ("Updated", sample_end),
            ],
        )
        if self.compact:
            self._sync_last_detail()

    def _sync_last_detail(self) -> None:
        ticker = self.last_ticker
        last = safe_float(ticker.get("c"), float("nan"))
        change = safe_float(ticker.get("P"), float("nan"))
        rows = [
            (
                "Last price",
                format_price(last) if math.isfinite(last) and last > 0 else "—",
            ),
            (
                "Mark price",
                format_price(self.mark) if math.isfinite(self.mark) and self.mark > 0 else "—",
            ),
            (
                "24h change",
                f"{change:+.2f}%" if math.isfinite(change) else "—",
            ),
        ]
        if self.compact:
            taker = self.cards["taker"].value.text().strip()
            long_short = self.cards["long_short"].value.text().strip()
            if taker and taker != "—":
                rows.append(("Taker buy share", taker))
            if long_short and long_short != "—":
                rows.append(("Long / short", long_short))
        self.cards["last"].set_detail("MARKET PRICE", rows)

    def update_ticker(self, ticker: dict[str, Any]) -> None:
        self.last_ticker = dict(ticker)
        price = safe_float(ticker.get("c"), float("nan"))
        volume = safe_float(ticker.get("q"), float("nan"))
        self.cards["last"].set_value(
            format_price(price) if math.isfinite(price) and price > 0 else "—",
            None,
        )
        self.cards["volume"].set_value(
            (_compact_market_notional(volume) if self.compact else human_number(volume, money=True))
            if math.isfinite(volume) and volume >= 0
            else "—",
            None,
        )
        base_volume = safe_float(ticker.get("v"), float("nan"))
        high = safe_float(ticker.get("h"), float("nan"))
        low = safe_float(ticker.get("l"), float("nan"))
        self.cards["volume"].set_detail(
            f"{perpetual_display_symbol(self.symbol)} · 24H VOLUME",
            [
                (
                    "Quote volume",
                    human_number(volume, money=True)
                    if math.isfinite(volume) and volume >= 0
                    else "—",
                ),
                (
                    "Base volume",
                    human_number(base_volume)
                    if math.isfinite(base_volume) and base_volume >= 0
                    else "—",
                ),
                ("24h high", format_price(high) if math.isfinite(high) and high > 0 else "—"),
                ("24h low", format_price(low) if math.isfinite(low) and low > 0 else "—"),
            ],
        )
        self._sync_last_detail()

    def update_mark(self, payload: dict[str, Any]) -> None:
        self.last_mark_payload = dict(payload)
        self.mark = safe_float(payload.get("p"))
        funding_rate = safe_float(payload.get("r"), float("nan"))
        funding = funding_rate * 100.0 if math.isfinite(funding_rate) else float("nan")
        if math.isfinite(funding) and funding > FUNDING_COLOR_THRESHOLD_PCT:
            funding_color = self.theme.get("metric_funding_positive", self.theme["green"])
        elif math.isfinite(funding) and funding < -FUNDING_COLOR_THRESHOLD_PCT:
            funding_color = self.theme.get("metric_funding_negative", self.theme["red"])
        else:
            funding_color = self.theme.get("metric_funding_neutral", self.theme["text"])
        rate_text = _market_funding_rate(funding)
        countdown, next_funding = _market_funding_time(
            safe_float(payload.get("T")), safe_float(payload.get("E")))
        funding_text = f"{rate_text} · {countdown}" if self.compact and countdown and math.isfinite(funding) else rate_text
        direction = "—"
        if math.isfinite(funding):
            direction = "Longs pay shorts" if funding > 0 else "Shorts pay longs" if funding < 0 else "No transfer"
        self.cards["funding"].set_value(
            funding_text if self.compact else f"{funding:+.4f}%" if math.isfinite(funding) else "—",
            funding_color if math.isfinite(funding) else None,
        )
        index_price = safe_float(payload.get("i"))
        self.cards["funding"].set_detail(
            f"{perpetual_display_symbol(self.symbol)} · FUNDING",
            [
                ("Funding rate", rate_text),
                ("Mark price", format_price(self.mark) if math.isfinite(self.mark) and self.mark > 0 else "—"),
                ("Index price", format_price(index_price) if math.isfinite(index_price) and index_price > 0 else "—"),
                ("Next funding", next_funding),
                ("Payment direction", direction),
            ],
        )
        self._sync_last_detail()

    def update_interest(self, payload: dict[str, Any], reference_price: float) -> None:
        self.last_interest_payload = dict(payload)
        self.last_interest_reference = reference_price
        units = safe_float(payload.get("openInterest"), float("nan"))
        reference = safe_float(reference_price, float("nan"))
        valid = (
            math.isfinite(units)
            and units >= 0
            and math.isfinite(reference)
            and reference > 0
        )
        notional = units * reference if valid else float("nan")
        self.cards["oi"].set_value(
            (_compact_market_notional(notional) if self.compact else human_number(notional, money=True))
            if math.isfinite(notional) else "—",
            None,
        )
        self.cards["oi"].set_detail(
            f"{perpetual_display_symbol(self.symbol)} · OPEN INTEREST",
            [
                (
                    "Open interest",
                    human_number(notional, money=True)
                    if math.isfinite(notional)
                    else "—",
                ),
                (
                    "Contracts",
                    human_number(units)
                    if math.isfinite(units) and units >= 0
                    else "—",
                ),
                (
                    "Reference price",
                    format_price(reference)
                    if math.isfinite(reference) and reference > 0
                    else "—",
                ),
            ],
        )

    def apply_theme(self, theme: dict[str, str]) -> None:
        self.theme = theme
        if self.metric_detail_popup is not None:
            self.metric_detail_popup.apply_theme(theme)
        for card in self.cards.values():
            if card.detail_popup is not None:
                card.detail_popup.apply_theme(theme)
        if self.last_ticker:
            self.update_ticker(self.last_ticker)
        if self.last_mark_payload:
            self.update_mark(self.last_mark_payload)
        if self.last_interest_payload:
            self.update_interest(self.last_interest_payload, self.last_interest_reference)
        if math.isfinite(self.last_taker_buy_pct):
            if self.last_taker_buy_pct > 65.0:
                color = self.theme.get("metric_taker_buy", self.theme["green"])
            elif self.last_taker_buy_pct < 35.0:
                color = self.theme.get("metric_taker_sell", self.theme["red"])
            else:
                color = self.theme.get("metric_taker_neutral", self.theme["text"])
            self.cards["taker"].set_value(f"{self.last_taker_buy_pct:.1f}%", color)
        ratio_text = self.cards["long_short"].value.text().strip()
        if ratio_text and ratio_text != "—":
            self.cards["long_short"].set_value(ratio_text, None)
        QtCore.QTimer.singleShot(0, self.cards["last"]._sync_identity_width)


class WatchlistWidget(QtWidgets.QWidget):
    symbol_selected = Signal(str)
    symbols_changed = Signal(object)
    tickers_changed = Signal()
    hour_changes_changed = Signal()
    current_changed = Signal(str)
    groups_changed = Signal(object)
    icon_missing = Signal(str)
    icon_changed = Signal(str)

    DEFAULTS: ClassVar[tuple[str, ...]] = (
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "BNBUSDT",
        "XRPUSDT",
        "DOGEUSDT",
        "ADAUSDT",
        "LINKUSDT",
        "AVAXUSDT",
        "SUIUSDT",
        "LTCUSDT",
        "1000PEPEUSDT",
    )

    def __init__(self, theme: dict[str, str], parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self.active_group = "Main"
        self.groups: dict[str, list[str]] = {self.active_group: list(self.DEFAULTS)}
        self.symbols = self.groups[self.active_group]
        self.tickers: dict[str, dict[str, Any]] = {}
        self.hour_changes: dict[str, float] = {}
        self.move_samples: dict[str, deque[tuple[float, float]]] = {}
        self._move_history_cache: dict[str, _WatchlistMoveHistory] = {}
        self.move_signal_until: dict[str, float] = {}
        self.move_signal_direction: dict[str, int] = {}
        self.move_last_trigger: dict[str, float] = {}
        self.current_symbol = DEFAULT_SYMBOL
        self._icon_requests_enabled = False
        self._icon_requests: set[str] = set()
        self._icon_retry_after: dict[str, float] = {}
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        controls = QtWidgets.QHBoxLayout()
        self.add_button = QtWidgets.QToolButton()
        self.add_button.setText("+ CURRENT")
        self.remove_button = QtWidgets.QToolButton()
        self.remove_button.setText("− REMOVE")
        controls.addWidget(self.add_button)
        controls.addWidget(self.remove_button)
        controls.addStretch()
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(("PAIR", "LAST", "1H", "24H"))
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.cellDoubleClicked.connect(self._open_row)
        self.add_button.clicked.connect(self.add_current)
        self.remove_button.clicked.connect(self.remove_selected)
        layout.addLayout(controls)
        layout.addWidget(self.table)
        self.refresh()

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)


        self.refresh(force=True)

    def set_current(self, symbol: str) -> None:
        self.current_symbol = self._canonical_symbol(symbol)
        self.refresh()
        self.current_changed.emit(self.current_symbol)

    @staticmethod
    def _canonical_symbol(symbol: str) -> str:
        value = str(symbol).upper().strip().replace("/", "").replace("-", "")
        return value.removesuffix(".P")

    def set_symbols(self, symbols: list[str], *, emit: bool = True) -> bool:
        normalized = list(
            dict.fromkeys(
                value
                for value in (self._canonical_symbol(symbol) for symbol in symbols)
                if value
            )
        )
        if normalized == self.symbols:
            return False
        self.symbols = normalized
        self.groups[self.active_group] = self.symbols
        self._discard_inactive_move_caches()
        self.refresh()
        if emit:
            self.symbols_changed.emit(list(self.symbols))
            self.groups_changed.emit(self.group_snapshot())
        return True

    def group_snapshot(self) -> dict[str, Any]:
        return {
            "active": self.active_group,
            "groups": {name: list(symbols) for name, symbols in self.groups.items()},
        }

    def set_groups(self, groups: dict[str, list[str]], active: str = "", *, emit: bool = True) -> bool:
        normalized: dict[str, list[str]] = {}
        for raw_name, raw_symbols in groups.items():
            name = str(raw_name).strip()[:32]
            if not name or name.casefold() in {value.casefold() for value in normalized}:
                continue
            normalized[name] = list(dict.fromkeys(
                value for value in (self._canonical_symbol(symbol) for symbol in raw_symbols)
                if value
            ))
        if not normalized:
            normalized = {"Main": list(self.DEFAULTS)}
        selected = next((name for name in normalized if name == active), next(iter(normalized)))
        changed = normalized != self.groups or selected != self.active_group
        self.groups = normalized
        self.active_group = selected
        self.symbols = self.groups[selected]
        self._discard_inactive_move_caches()
        self.refresh()
        if changed and emit:
            self.groups_changed.emit(self.group_snapshot())
            self.symbols_changed.emit(list(self.symbols))
        return changed

    def set_active_group(self, name: str) -> bool:
        if name not in self.groups or name == self.active_group:
            return False
        self.active_group = name
        self.symbols = self.groups[name]
        self._discard_inactive_move_caches()
        self.refresh()
        self.groups_changed.emit(self.group_snapshot())
        self.symbols_changed.emit(list(self.symbols))
        return True

    def create_group(self, name: str) -> bool:
        clean = str(name).strip()[:32]
        if not clean or any(clean.casefold() == value.casefold() for value in self.groups):
            return False
        self.groups[clean] = []
        self.groups_changed.emit(self.group_snapshot())
        return self.set_active_group(clean)

    def rename_active_group(self, name: str) -> bool:
        clean = str(name).strip()[:32]
        if not clean or clean == self.active_group or any(
            clean.casefold() == value.casefold() for value in self.groups
        ):
            return False
        rebuilt: dict[str, list[str]] = {}
        for group, symbols in self.groups.items():
            rebuilt[clean if group == self.active_group else group] = symbols
        self.groups = rebuilt
        self.active_group = clean
        self.symbols = self.groups[clean]
        self.groups_changed.emit(self.group_snapshot())
        return True

    def delete_active_group(self) -> bool:
        if len(self.groups) <= 1:
            return False
        del self.groups[self.active_group]
        self.active_group = next(iter(self.groups))
        self.symbols = self.groups[self.active_group]
        self._discard_inactive_move_caches()
        self.refresh()
        self.groups_changed.emit(self.group_snapshot())
        self.symbols_changed.emit(list(self.symbols))
        return True

    def set_icon_requests_enabled(self, enabled: bool) -> None:
        self._icon_requests_enabled = bool(enabled)
        if self._icon_requests_enabled:
            self.request_missing_icons()

    def request_icon(self, symbol: str) -> None:
        if not self._icon_requests_enabled:
            return
        base = coin_base_symbol(symbol)
        if not base:
            return
        if coin_icon_exists(base):
            self._icon_requests.discard(base)
            self._icon_retry_after.pop(base, None)
            return
        now = time.monotonic()
        if base in self._icon_requests or now < self._icon_retry_after.get(base, 0.0):
            return
        self._icon_requests.add(base)
        self.icon_missing.emit(base)

    def request_missing_icons(self) -> None:
        if not self._icon_requests_enabled:
            return
        symbols = dict.fromkeys(
            symbol
            for group in self.groups.values()
            for symbol in group
        )
        for symbol in symbols:
            if not coin_icon_exists(symbol):
                self.request_icon(symbol)

    def icon_lookup_finished(
        self,
        symbol: str,
        found: bool,
        *,
        retry_seconds: float = 3600.0,
    ) -> None:
        base = coin_base_symbol(symbol)
        if not base:
            return
        self._icon_requests.discard(base)
        if found and coin_icon_exists(base):
            self._icon_retry_after.pop(base, None)
            invalidate_watchlist_coin_icon(base)
            self.icon_changed.emit(base)
            return
        self._icon_retry_after[base] = time.monotonic() + max(30.0, float(retry_seconds))

    def contains(self, symbol: str) -> bool:
        return self._canonical_symbol(symbol) in self.symbols

    def set_symbol_watched(self, symbol: str, watched: bool) -> bool:
        target = self._canonical_symbol(symbol)
        if not target:
            return False
        if (target in self.symbols) == bool(watched):
            return False
        updated = [value for value in self.symbols if value != target]
        if watched:
            updated.append(target)
        return self.set_symbols(updated)

    def add_current(self) -> None:
        self.set_symbol_watched(self.current_symbol, True)

    def remove_selected(self) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self.symbols):
            self.remove_symbol(self.symbols[row])

    def remove_symbol(self, symbol: str) -> None:
        self.set_symbol_watched(symbol, False)

    def _open_row(self, row: int, _column: int) -> None:
        if 0 <= row < len(self.symbols):
            self.symbol_selected.emit(self.symbols[row])

    def update_tickers(self, updates: list[dict[str, Any]]) -> None:
        changed = False
        for ticker in updates:
            symbol = ticker.get("s")
            if symbol in self.symbols:
                self.tickers[symbol] = ticker
                self._observe_move(symbol, ticker)
                changed = True
        if changed:
            self.refresh()
            self.tickers_changed.emit()

    def _discard_inactive_move_caches(self) -> None:
        active = set(self.symbols)
        for symbol in tuple(self._move_history_cache):
            if symbol not in active:
                self._move_history_cache.pop(symbol, None)

    def _observe_move(self, symbol: str, ticker: dict[str, Any]) -> None:
        """Detect unusual five-minute moves relative to recent behavior/liquidity."""
        price = safe_float(ticker.get("c"))
        if price <= 0:
            return
        now = time.monotonic()
        samples = self.move_samples.setdefault(symbol, deque(maxlen=420))
        sample = (now, price)
        replace = bool(samples and now - samples[-1][0] < 5.0)
        cache = self._move_history_cache.get(symbol)
        if cache is not None and not cache.matches(samples):
            cache = None
        incremental = cache is not None and cache.can_update(
            samples, sample, replace=replace
        )
        if replace:
            samples[-1] = (now, price)
        else:
            samples.append(sample)
        cutoff = now - 1_800.0
        while samples and samples[0][0] < cutoff:
            samples.popleft()
        if cache is None or not incremental:
            cache = _WatchlistMoveHistory(samples)
        else:
            if replace:
                cache.replace_last(sample)
            else:
                cache.append(sample)
            if not cache.incremental_safe:
                cache = _WatchlistMoveHistory(samples)
            else:
                removed_count = len(cache.rows) - len(samples)
                if removed_count > 0:
                    cache.prune_prefix(removed_count)
        self._move_history_cache[symbol] = cache
        reference = cache.reference(now)
        if reference is None:
            return
        if now - reference[0] < 270.0 or reference[1] <= 0:
            return
        move = (price / reference[1] - 1.0) * 100.0
        baseline = cache.baseline
        quote_volume = safe_float(ticker.get("q"))
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
        if abs(move) < threshold or now - self.move_last_trigger.get(symbol, 0.0) < 30.0:
            return
        self.move_last_trigger[symbol] = now
        self.move_signal_until[symbol] = now + 30.0
        self.move_signal_direction[symbol] = 1 if move > 0 else -1

    def move_signal(self, symbol: str) -> int:
        if self.move_signal_until.get(symbol, 0.0) <= time.monotonic():
            return 0
        return self.move_signal_direction.get(symbol, 0)

    def set_hour_changes(self, values: dict[str, float]) -> None:
        changed = False
        for symbol, value in values.items():
            if symbol not in self.symbols:
                continue
            numeric = safe_float(value)
            if self.hour_changes.get(symbol) != numeric:
                self.hour_changes[symbol] = numeric
                changed = True
        stale = [symbol for symbol in self.hour_changes if symbol not in self.symbols]
        for symbol in stale:
            del self.hour_changes[symbol]
            changed = True
        if changed:
            self.refresh()
            self.hour_changes_changed.emit()

    def refresh(self, *, force: bool = False) -> None:


        if not force and not self.isVisible():
            return
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(self.symbols))
        current_bg = QtGui.QColor(
            self.theme.get("active", self.theme.get("panel2", self.theme["bg"]))
        )
        for row, symbol in enumerate(self.symbols):
            ticker = self.tickers.get(symbol, {})
            price = safe_float(ticker.get("c"))
            change_1h = self.hour_changes.get(symbol)
            change_24h = safe_float(ticker.get("P"))
            values = (
                symbol.removesuffix("USDT") + " / USDT",
                format_price(price) if price else "—",
                f"{change_1h:+.2f}%" if change_1h is not None else "—",
                f"{change_24h:+.2f}%" if ticker else "—",
            )
            for column, value in enumerate(values):
                item = self.table.item(row, column)
                if item is None:
                    item = QtWidgets.QTableWidgetItem()
                    item.setFont(typography_font(TextRole.INSTRUMENT_SYMBOL if column == 0 else TextRole.TABLE_VALUE))
                    self.table.setItem(row, column, item)
                if item.text() != value:
                    item.setText(value)
                item.setBackground(
                    current_bg if symbol == self.current_symbol else QtGui.QBrush()
                )
                if column == 2 and change_1h is not None:
                    item.setForeground(
                        QtGui.QColor(
                            self.theme["green"] if change_1h >= 0 else self.theme["red"]
                        )
                    )
                elif column == 3 and ticker:
                    item.setForeground(
                        QtGui.QColor(
                            self.theme["green"] if change_24h >= 0 else self.theme["red"]
                        )
                    )
                else:
                    item.setForeground(QtGui.QBrush())
                if column > 0:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
            self.table.setRowHeight(row, 24)
        current_row = next(
            (
                row
                for row, symbol in enumerate(self.symbols)
                if symbol == self.current_symbol
            ),
            -1,
        )
        if current_row >= 0:
            blocker = QtCore.QSignalBlocker(self.table)
            self.table.selectRow(current_row)
            del blocker
        else:
            self.table.clearSelection()
        self.table.setUpdatesEnabled(True)

    def apply_theme(self, theme: dict[str, str]) -> None:
        self.theme = theme
        self.refresh()


class WatchlistHeaderView(QtWidgets.QHeaderView):
    """Watchlist header with a membership toggle in its leading section."""

    toggle_current_requested = Signal()
    width_environment_changed = Signal()

    def __init__(
        self,
        theme: dict[str, str],
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.theme = theme
        self._toggle_hovered = False
        self._toggle_pressed = False
        self.setMouseTracking(True)
        self.setToolTip("Add or remove the current chart symbol")

    def event(self, event: QtCore.QEvent) -> bool:
        result = super().event(event)
        if _watchlist_width_environment_changed(event):
            self.width_environment_changed.emit()
        return result

    def _toggle_rect(self) -> QtCore.QRect:
        position = self.sectionViewportPosition(0)
        width = self.sectionSize(0)
        return QtCore.QRect(position, 0, width, self.height())

    def paintSection(
        self,
        painter: QtGui.QPainter,
        rect: QtCore.QRect,
        logical_index: int,
    ) -> None:
        super().paintSection(painter, rect, logical_index)
        if logical_index != 0:
            return
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        color = self.theme["cyan"] if self._toggle_hovered else self.theme["text"]
        pen = QtGui.QPen(QtGui.QColor(color), 1.45)
        pen.setCosmetic(True)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        center = QtCore.QPointF(rect.center())
        painter.drawLine(
            QtCore.QPointF(center.x() - 4.0, center.y()),
            QtCore.QPointF(center.x() + 4.0, center.y()),
        )
        painter.drawLine(
            QtCore.QPointF(center.x(), center.y() - 4.0),
            QtCore.QPointF(center.x(), center.y() + 4.0),
        )
        painter.restore()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        hovered = self._toggle_rect().contains(event.position().toPoint())
        if hovered != self._toggle_hovered:
            self._toggle_hovered = hovered
            self.setCursor(
                Qt.CursorShape.PointingHandCursor
                if hovered
                else Qt.CursorShape.ArrowCursor
            )
            self.updateSection(0)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        self._toggle_hovered = False
        self._toggle_pressed = False
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.updateSection(0)
        super().leaveEvent(event)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._toggle_rect().contains(event.position().toPoint())
        ):
            self._toggle_pressed = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._toggle_pressed and event.button() == Qt.MouseButton.LeftButton:
            self._toggle_pressed = False
            if self._toggle_rect().contains(event.position().toPoint()):
                self.toggle_current_requested.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def apply_theme(self, theme: dict[str, str]) -> None:
        self.theme = theme
        self.updateSection(0)


class WatchlistTableWidget(QtWidgets.QTableWidget):
    """Table whose internal row drag delegates ordering to canonical state."""

    row_move_requested = Signal(int, int)
    row_remove_requested = Signal(int)
    width_environment_changed = Signal()

    def event(self, event: QtCore.QEvent) -> bool:
        result = super().event(event)
        if _watchlist_width_environment_changed(event):
            self.width_environment_changed.emit()
        return result

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            index = self.indexAt(event.position().toPoint())
            if index.isValid():
                self.row_remove_requested.emit(index.row())
                event.accept()
                return
        super().mousePressEvent(event)

    def dropEvent(self, event: QtGui.QDropEvent) -> None:
        source_row = self.currentRow()
        target_index = self.indexAt(event.position().toPoint())
        target_row = target_index.row() if target_index.isValid() else self.rowCount()
        if (
            target_index.isValid()
            and self.dropIndicatorPosition()
            == QtWidgets.QAbstractItemView.DropIndicatorPosition.BelowItem
        ):
            target_row += 1
        if source_row < target_row:
            target_row -= 1
        target_row = max(0, min(self.rowCount() - 1, target_row))
        if source_row >= 0 and target_row != source_row:
            self.row_move_requested.emit(source_row, target_row)
        event.acceptProposedAction()


class WatchlistSidebarWidget(QtWidgets.QWidget):
    """Compact sidebar view backed by the canonical watchlist."""

    symbol_selected = Signal(str)
    toggle_current_requested = Signal()

    def __init__(
        self,
        source: WatchlistWidget,
        parent: QtWidgets.QWidget | None = None,
    ):
        super().__init__(parent)
        self.setMinimumSize(0, 0)
        self.source = source
        self._sort_column: int | None = None
        self._sort_descending = False
        self._sorting = False
        self._row_display_signatures = {}
        self._icon_refresh_after = {}
        self._theme_revision = 0
        self._last_size_style_key = None
        self._pending_numeric_resizes: set[int] = set()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(0)

        group_bar = QtWidgets.QHBoxLayout()
        group_bar.setContentsMargins(0, 0, 0, 3)
        group_bar.setSpacing(4)
        self.group_selector = QtWidgets.QComboBox()
        self.group_selector.setObjectName("watchlistGroupSelector")
        self.group_selector.currentTextChanged.connect(self._select_group)
        self.group_menu_button = QtWidgets.QToolButton()
        self.group_menu_button.setObjectName("watchlistGroupMenu")
        set_text_role(self.group_menu_button, TextRole.UI_GLYPH)
        self.group_menu_button.setText("⋯")
        self.group_menu_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.group_menu_button.setToolTip("Manage watchlist groups")
        group_menu = QtWidgets.QMenu(self.group_menu_button)
        group_menu.addAction("New group…", self._new_group)
        group_menu.addAction("Rename group…", self._rename_group)
        group_menu.addAction("Delete group", self._delete_group)
        self.group_menu_button.setMenu(group_menu)
        group_bar.addWidget(self.group_selector, 1)
        group_bar.addWidget(self.group_menu_button)
        layout.addLayout(group_bar)

        self.table = WatchlistTableWidget(0, 5)
        self.table.setObjectName("sidebarWatchlistTable")
        self.watchlist_header = WatchlistHeaderView(source.theme, self.table)
        self.table.setHorizontalHeader(self.watchlist_header)
        self.table.setHorizontalHeaderLabels(("", "PAIR", "LAST", "1H", "24H"))
        self.table.setIconSize(QtCore.QSize(18, 18))
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Fixed)
        header.resizeSection(0, 26)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for column in (2, 3, 4):
            header.setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeMode.Fixed)
        header.setMinimumSectionSize(26)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(False)
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        pair_header = self.table.horizontalHeaderItem(1)
        if pair_header is not None:
            pair_header.setTextAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        self.table.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setMinimumHeight(0)
        self.table.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Ignored,
        )
        self.table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
        )
        self.table.setDragEnabled(True)
        self.table.setAcceptDrops(True)
        self.table.setDropIndicatorShown(True)
        self.table.setDragDropOverwriteMode(False)
        self.table.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.table.setDragDropMode(
            QtWidgets.QAbstractItemView.DragDropMode.InternalMove
        )
        self.table.setToolTip(
            "Double-click to open · right-click to remove · drag rows to reorder · header + toggles the current pair"
        )
        self.table.width_environment_changed.connect(self._width_environment_changed)
        self.watchlist_header.width_environment_changed.connect(
            self._width_environment_changed
        )

        layout.addWidget(self.table, 1)
        self.watchlist_header.toggle_current_requested.connect(
            self.toggle_current_requested.emit
        )
        self.table.row_move_requested.connect(self._move_row)
        self.table.row_remove_requested.connect(self._remove_row)
        self.table.cellDoubleClicked.connect(self._open_row)
        header.sectionClicked.connect(self._sort_clicked)
        self._ticker_refresh_pending = False

        self.source.symbols_changed.connect(self._source_symbols_changed)
        self.source.tickers_changed.connect(self._schedule_ticker_refresh)
        self.source.hour_changes_changed.connect(self._hour_state_changed)
        self.source.current_changed.connect(self._sync_current_symbol)
        self.source.groups_changed.connect(self._groups_changed)
        self.source.icon_changed.connect(self._icon_changed)
        typography_controller().changed.connect(self._typography_changed)
        self._groups_changed(self.source.group_snapshot())
        self.refresh()

    def _width_environment_changed(self) -> None:
        self._pending_numeric_resizes.update((2, 3, 4))
        self.refresh()

    def _typography_changed(self) -> None:
        for row in range(self.table.rowCount()):
            for column in range(1, 5):
                item = self.table.item(row, column)
                if item is not None:
                    role = TextRole.INSTRUMENT_SYMBOL if column == 1 else TextRole.TABLE_VALUE
                    item.setFont(typography_font(role))
        self._pending_numeric_resizes.update((2, 3, 4))
        self.refresh()

    def _groups_changed(self, _snapshot: object = None) -> None:
        blocker = QtCore.QSignalBlocker(self.group_selector)
        self.group_selector.clear()
        self.group_selector.addItems(tuple(self.source.groups))
        self.group_selector.setCurrentText(self.source.active_group)
        del blocker
        self.group_menu_button.menu().actions()[-1].setEnabled(len(self.source.groups) > 1)
        self.refresh()

    def _select_group(self, name: str) -> None:
        if name:
            self._clear_sort()
            self.source.set_active_group(name)

    def _new_group(self) -> None:
        name, accepted = QtWidgets.QInputDialog.getText(self, "New watchlist group", "Name")
        if accepted and not self.source.create_group(name):
            QtWidgets.QMessageBox.warning(self, "Watchlist group", "Use a unique, non-empty group name.")

    def _rename_group(self) -> None:
        name, accepted = QtWidgets.QInputDialog.getText(
            self, "Rename watchlist group", "Name", text=self.source.active_group
        )
        if accepted and name.strip() != self.source.active_group and not self.source.rename_active_group(name):
            QtWidgets.QMessageBox.warning(self, "Watchlist group", "Use a unique, non-empty group name.")

    def _delete_group(self) -> None:
        self.source.delete_active_group()

    def _remove_row(self, row: int) -> None:
        item = self.table.item(row, 1)
        symbol = str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""
        if symbol:
            self.source.remove_symbol(symbol)

    def selected_symbol(self) -> str:
        row = self.table.currentRow()
        item = self.table.item(row, 1) if row >= 0 else None
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""

    def select_symbol(self, symbol: str) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 1)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == symbol:
                self.table.setCurrentCell(row, 1)
                self.table.selectRow(row)
                self.table.scrollToItem(
                    item,
                    QtWidgets.QAbstractItemView.ScrollHint.EnsureVisible,
                )
                return
        self.table.setCurrentCell(-1, -1)
        self.table.clearSelection()

    def _sync_current_symbol(self, symbol: str) -> None:
        del symbol
        self.refresh()

    def _open_row(self, row: int, _column: int) -> None:
        if _column == 0:
            return
        item = self.table.item(row, 1)
        symbol = str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""
        if symbol:
            self.symbol_selected.emit(symbol)

    def _move_row(self, source_row: int, target_row: int) -> None:
        symbols = list(self.source.symbols)
        if not (0 <= source_row < len(symbols) and 0 <= target_row < len(symbols)):
            return
        symbol = symbols.pop(source_row)
        symbols.insert(target_row, symbol)
        self._clear_sort()
        self.source.set_symbols(symbols)

    def _clear_sort(self) -> None:
        self._sort_column = None
        self._sort_descending = False
        self.table.horizontalHeader().setSortIndicatorShown(False)

    def _sorted_symbols(self) -> list[str]:
        symbols = list(self.source.symbols)
        if self._sort_column == 1:
            return sorted(symbols, key=str.casefold, reverse=self._sort_descending)
        if self._sort_column == 3:
            known = [symbol for symbol in symbols if symbol in self.source.hour_changes]
            missing = [symbol for symbol in symbols if symbol not in self.source.hour_changes]
            known.sort(
                key=lambda symbol: self.source.hour_changes.get(symbol, 0.0),
                reverse=self._sort_descending,
            )
            return known + missing
        if self._sort_column == 4:
            known = [symbol for symbol in symbols if symbol in self.source.tickers]
            missing = [symbol for symbol in symbols if symbol not in self.source.tickers]
            known.sort(
                key=lambda symbol: safe_float(
                    self.source.tickers.get(symbol, {}).get("P")
                ),
                reverse=self._sort_descending,
            )
            return known + missing
        return symbols

    def _sort_clicked(self, column: int) -> None:
        if column not in {1, 3, 4}:
            return
        if self._sort_column == column:
            self._sort_descending = not self._sort_descending
        else:
            self._sort_column = column
            self._sort_descending = False
        self.table.horizontalHeader().setSortIndicatorShown(False)
        ordered = self._sorted_symbols()
        self._sorting = True
        try:
            if not self.source.set_symbols(ordered):
                self.refresh()
        finally:
            self._sorting = False

    def _source_symbols_changed(self, _symbols: object) -> None:
        if self._sorting or self._sort_column is None:
            self.refresh()
            return
        ordered = self._sorted_symbols()
        self._sorting = True
        try:
            if not self.source.set_symbols(ordered):
                self.refresh()
        finally:
            self._sorting = False

    def _schedule_ticker_refresh(self) -> None:
        """Commit source ticker changes in the same application presentation frame."""
        if not self.isVisible():
            self._ticker_refresh_pending = True
            return
        self._ticker_refresh_pending = False
        self._ticker_state_changed()

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        if self._ticker_refresh_pending:
            self._ticker_refresh_pending = False
            self._ticker_state_changed()

    def _ticker_state_changed(self) -> None:
        if self._sort_column != 4:
            self.refresh()
            return
        ordered = self._sorted_symbols()
        self._sorting = True
        try:
            if not self.source.set_symbols(ordered):
                self.refresh()
        finally:
            self._sorting = False

    def _hour_state_changed(self) -> None:
        if self._sort_column != 3:
            self.refresh()
            return
        ordered = self._sorted_symbols()
        self._sorting = True
        try:
            if not self.source.set_symbols(ordered):
                self.refresh()
        finally:
            self._sorting = False

    def _icon_changed(self, base: str) -> None:
        base = coin_base_symbol(base)
        if not base:
            return
        for row in range(self.table.rowCount()):
            pair_item = self.table.item(row, 1)
            symbol = str(pair_item.data(Qt.ItemDataRole.UserRole) or "") if pair_item else ""
            if coin_base_symbol(symbol) != base:
                continue
            icon_item = self.table.item(row, 0)
            if icon_item is not None:
                icon_item.setIcon(watchlist_coin_icon(symbol, self.source.theme))
                self._row_display_signatures.pop(row, None)
                self._icon_refresh_after[symbol] = float("inf")

    def refresh(self) -> None:
        previous_selected = self.selected_symbol()
        symbols = list(self.source.symbols)
        active_symbol = (
            self.source.current_symbol
            if self.source.current_symbol in symbols
            else ""
        )
        selection_symbol = active_symbol or previous_selected
        dirty_columns = set(self._pending_numeric_resizes)
        self._pending_numeric_resizes.clear()
        if self.table.rowCount() != len(symbols):
            dirty_columns.update((2, 3, 4))
            self.table.setRowCount(len(symbols))
        now = time.monotonic()
        signatures = {}
        style_key = (self._theme_revision, self.devicePixelRatioF())
        if style_key != self._last_size_style_key:
            dirty_columns.update((2, 3, 4))
            self._last_size_style_key = style_key
        selected_row = -1
        current_bg = QtGui.QColor(
            self.source.theme.get(
                "active",
                self.source.theme.get("panel2", self.source.theme["bg"]),
            )
        )
        for row, symbol in enumerate(symbols):
            if symbol == selection_symbol:
                selected_row = row
            ticker = self.source.tickers.get(symbol, {})
            move_direction = self.source.move_signal(symbol)
            price = safe_float(ticker.get("c"))
            change_1h = self.source.hour_changes.get(symbol)
            change_24h = safe_float(ticker.get("P"))
            signature = (symbol, price, change_1h, change_24h, bool(ticker),
                         move_direction, symbol == active_symbol, style_key)
            previous = self._row_display_signatures.get(row)
            signatures[row] = signature
            icon_due = now >= self._icon_refresh_after.get(symbol, 0.0)
            if signature == previous and not icon_due:
                continue
            static_changed = previous is None or (previous[0], previous[-1]) != (symbol, style_key)
            icon_item = self.table.item(row, 0)
            if icon_item is None:
                icon_item = QtWidgets.QTableWidgetItem()
                icon_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, 0, icon_item)
            if static_changed or icon_due:
                found = coin_icon_exists(symbol)
                icon_item.setIcon(watchlist_coin_icon(symbol, self.source.theme))
                self._icon_refresh_after[symbol] = float("inf") if found else now + 60.0
                if not found:
                    self.source.request_icon(symbol)
                icon_item.setToolTip(f"{symbol} · right-click row to remove")
            icon_item.setBackground(
                current_bg if symbol == active_symbol else QtGui.QBrush()
            )

            quote = "USDC" if symbol.endswith("USDC") else "USDT"
            base = symbol.removesuffix(quote)
            display = f"{base}{quote}.P"
            values = (
                display,
                format_price(price) if price else "—",
                f"{change_1h:+.2f}%" if change_1h is not None else "—",
                f"{change_24h:+.2f}%" if ticker else "—",
            )
            tooltip = (
                f"{display} · Last {format_price(price) if price else '—'} · "
                f"1h {change_1h:+.2f}% · 24h {change_24h:+.2f}%"
                if ticker and change_1h is not None
                else f"{display}"
            )
            for column, value in enumerate(values, start=1):
                item = self.table.item(row, column)
                if item is None:
                    item = QtWidgets.QTableWidgetItem()
                    item.setFont(typography_font(TextRole.INSTRUMENT_SYMBOL if column == 1 else TextRole.TABLE_VALUE))
                    self.table.setItem(row, column, item)
                if item.text() != value:
                    item.setText(value)
                    if column in (2, 3, 4):
                        dirty_columns.add(column)
                if item.toolTip() != tooltip:
                    item.setToolTip(tooltip)
                item.setBackground(
                    current_bg
                    if symbol == active_symbol
                    else QtGui.QBrush()
                )
                if column == 3 and move_direction:
                    signal_color = self.source.theme[
                        "green" if move_direction > 0 else "red"
                    ]
                    item.setBackground(alpha_color(signal_color, 46))
                    item.setToolTip(
                        f"{tooltip} · unusual five-minute move detected"
                    )
                if column == 1:
                    item.setData(Qt.ItemDataRole.UserRole, symbol)
                    item.setForeground(
                        QtGui.QColor(self.source.theme["cyan"])
                        if symbol == active_symbol
                        else QtGui.QBrush()
                    )
                elif column == 3 and change_1h is not None:
                    item.setForeground(
                        QtGui.QColor(
                            self.source.theme["green"]
                            if change_1h >= 0
                            else self.source.theme["red"]
                        )
                    )
                elif column == 4 and ticker:
                    item.setForeground(
                        QtGui.QColor(
                            self.source.theme["green"]
                            if change_24h >= 0
                            else self.source.theme["red"]
                        )
                    )
                else:
                    item.setForeground(QtGui.QBrush())
                if column > 1:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight
                        | Qt.AlignmentFlag.AlignVCenter
                    )
            if static_changed:
                self.table.setRowHeight(row, 23)
        self._row_display_signatures = signatures
        if selected_row >= 0 and (
            self.table.currentRow() != selected_row
            or self.selected_symbol() != symbols[selected_row]
        ):
            blocker = QtCore.QSignalBlocker(self.table)
            self.table.setCurrentCell(selected_row, 1)
            self.table.selectRow(selected_row)
            del blocker
        elif selected_row < 0 and self.table.currentRow() >= 0:
            self.table.setCurrentCell(-1, -1)
            self.table.clearSelection()
        for column in sorted(dirty_columns):
            self.table.resizeColumnToContents(column)

    def apply_theme(self, theme: dict[str, str]) -> None:
        self._theme_revision += 1
        self.watchlist_header.apply_theme(theme)
        self._pending_numeric_resizes.update((2, 3, 4))
        self.refresh()


class SymbolSearchDialog(QtWidgets.QDialog):
    filter_requested = Signal()
    sort_changed = Signal(str)

    def __init__(
        self,
        symbols: list[str],
        tickers: dict[str, dict[str, Any]],
        theme: dict[str, str],
        initial_text: str = "",
        sort_mode: str = "gainers",
        filter_timeframe: str = "24h",
        minimum_volume: float = 0.0,
        volume_values: dict[str, float] | None = None,
        parent: QtWidgets.QWidget | None = None,
        hour_changes: dict[str, float] | None = None,
    ):
        super().__init__(parent)
        self.hour_changes = dict(hour_changes or {})
        self.symbols = list(symbols)
        self.tickers = dict(tickers)
        self.theme = theme
        self.sort_mode = sort_mode if sort_mode in MARKET_SORT_MODES else "gainers"
        self.filter_timeframe = filter_timeframe
        self.minimum_volume = max(0.0, minimum_volume)
        self.volume_values = dict(volume_values or {})
        self.use_volume_override = volume_values is not None
        self.selected_symbol = ""
        self.setObjectName("symbolSearchDialog")
        self.setWindowTitle("Search Binance USD-M markets")
        self.setModal(True)
        self.resize(850, 560)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(9)
        heading_row = QtWidgets.QHBoxLayout()
        heading = QtWidgets.QLabel("SYMBOL SEARCH")
        heading.setObjectName("dialogHeading")
        hint = QtWidgets.QLabel("TYPE TO FILTER  ·  ENTER TO OPEN  ·  ESC TO CLOSE")
        hint.setObjectName("subtleLabel")
        hint.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        heading_row.addWidget(heading)
        heading_row.addStretch(1)
        heading_row.addWidget(hint)
        search_row = QtWidgets.QHBoxLayout()
        search_row.setSpacing(7)
        self.search = QtWidgets.QLineEdit()
        self.search.setObjectName("globalSymbolSearch")
        self.search.setPlaceholderText("Search BTC, ETH, PEPE…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedHeight(38)
        self.search.installEventFilter(self)
        self.filter_button = QtWidgets.QToolButton()
        self.filter_button.setObjectName("searchFilterButton")
        self.filter_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.filter_button.setFixedHeight(38)
        self.filter_button.setMinimumWidth(116)
        self.filter_button.setMaximumWidth(180)
        self.filter_button.setAccessibleName("Market volume filter")
        self.filter_button.clicked.connect(
            lambda _checked=False: self.filter_requested.emit()
        )
        search_row.addWidget(self.search, 1)
        search_row.addWidget(self.filter_button)
        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setObjectName("symbolSearchResults")
        self.table.setHorizontalHeaderLabels(("PAIR", "LAST", "1H CHANGE", "24H CHANGE", "24H USDT VOLUME"))
        header = self.table.horizontalHeader()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(False)
        header.sectionClicked.connect(self._header_clicked)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for column, width in ((1, 140), (2, 110), (3, 110), (4, 165)):
            header.setSectionResizeMode(
                column,
                QtWidgets.QHeaderView.ResizeMode.Fixed,
            )
            self.table.setColumnWidth(column, width)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.itemActivated.connect(self._accept_item)
        self.table.cellDoubleClicked.connect(
            lambda row, _column: self._accept_row(row)
        )
        self.search.textChanged.connect(self._refresh)
        self.search.returnPressed.connect(self._accept_current)
        self.result_count = QtWidgets.QLabel()
        self.result_count.setObjectName("searchResultCount")
        layout.addLayout(heading_row)
        layout.addLayout(search_row)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.result_count)
        self.search.setText(initial_text)
        self.search.setCursorPosition(len(initial_text))
        self._set_sort_indicator()
        self.set_filter_state(filter_timeframe, minimum_volume, volume_values)
        self._refresh(initial_text)
        self.setFocusProxy(self.search)

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        self.search.setFocus(Qt.FocusReason.OtherFocusReason)
        self.search.deselect()
        self.search.setCursorPosition(len(self.search.text()))

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if watched is self.search and event.type() == QtCore.QEvent.Type.KeyPress:
            key = int(event.key())
            if key in {int(Qt.Key.Key_Down), int(Qt.Key.Key_Up)} and self.table.rowCount():
                step = 1 if key == int(Qt.Key.Key_Down) else -1
                row = max(0, min(self.table.rowCount() - 1, self.table.currentRow() + step))
                self.table.setCurrentCell(row, 0)
                self.table.scrollToItem(self.table.item(row, 0))
                return True
            if key in {int(Qt.Key.Key_Return), int(Qt.Key.Key_Enter)}:
                self._accept_current()
                return True
        return super().eventFilter(watched, event)

    def _volume_for(self, symbol: str) -> float:
        if self.use_volume_override:
            return safe_float(self.volume_values.get(symbol))
        return safe_float(self.tickers.get(symbol, {}).get("q"))

    def _set_sort_indicator(self) -> None:
        self.table.horizontalHeader().setSortIndicatorShown(False)

    def set_sort_mode(self, mode: str) -> None:
        if mode not in MARKET_SORT_MODES:
            return
        self._query_volume_sort = False
        self.sort_mode = mode
        self._set_sort_indicator()
        self._refresh(self.search.text())

    def _header_clicked(self, column: int) -> None:
        if column == 0:
            mode = "symbol_desc" if self.sort_mode == "symbol" else "symbol"
        elif column == 1:
            mode = "price_asc" if self.sort_mode == "price" else "price"
        elif column == 2:
            mode = "hour_losers" if self.sort_mode == "hour_gainers" else "hour_gainers"
        elif column == 3:
            mode = "losers" if self.sort_mode == "gainers" else "gainers"
        else:
            mode = "volume_asc" if self.sort_mode == "volume" else "volume"
        self.set_sort_mode(mode)
        self.sort_changed.emit(mode)

    def set_filter_state(
        self,
        timeframe: str,
        minimum_volume: float,
        volume_values: dict[str, float] | None,
    ) -> None:
        self.filter_timeframe = timeframe
        self.minimum_volume = max(0.0, minimum_volume)
        self.volume_values = dict(volume_values or {})
        self.use_volume_override = volume_values is not None
        active = self.minimum_volume > 0
        filter_text = (
            f"{timeframe.upper()}  ≥  {human_number(self.minimum_volume)}"
            if active
            else "FILTER"
        )
        if self.filter_button.text() != filter_text:
            self.filter_button.setText(filter_text)
        style_state_changed = self.filter_button.property("active") != active
        if style_state_changed:
            self.filter_button.setProperty("active", active)
        self.filter_button.setIcon(
            line_icon(
                "filter",
                self.theme["cyan"] if active else self.theme["muted"],
                self.devicePixelRatioF(),
            )
        )
        self.filter_button.setIconSize(QtCore.QSize(17, 17))
        self.filter_button.setToolTip(
            f"Active filter · {timeframe} USDT volume ≥ {human_number(self.minimum_volume, money=True)}"
            if active
            else "Set a minimum 1h, 4h or 24h USDT trading volume"
        )
        if style_state_changed:
            style = self.filter_button.style()
            style.unpolish(self.filter_button)
            style.polish(self.filter_button)
        self.table.horizontalHeaderItem(4).setText(
            f"{timeframe.upper()} USDT VOLUME"
        )

    def set_market_data(
        self,
        symbols: list[str],
        tickers: dict[str, dict[str, Any]],
        sort_mode: str,
        timeframe: str,
        minimum_volume: float,
        volume_values: dict[str, float] | None,
    ) -> None:
        self.symbols = list(symbols)
        self.tickers = dict(tickers)
        self.sort_mode = sort_mode if sort_mode in MARKET_SORT_MODES else "gainers"
        self.set_filter_state(timeframe, minimum_volume, volume_values)
        self._set_sort_indicator()
        self._refresh(self.search.text())

    def set_filter_loading(self, percent: int | None) -> None:
        if percent is None:
            self.filter_button.setEnabled(True)
            self.set_filter_state(
                self.filter_timeframe,
                self.minimum_volume,
                self.volume_values if self.use_volume_override else None,
            )
            return
        self.filter_button.setEnabled(False)
        self.filter_button.setText(f"VOLUME  {max(0, min(99, percent))}%")

    def set_hour_changes(self, values: dict[str, float]) -> None:
        self.hour_changes.update({symbol: value for symbol, value in values.items() if math.isfinite(value)})
        self._refresh(self.search.text())

    def update_tickers(self, updates: list[dict[str, Any]]) -> None:
        for ticker in updates:
            symbol = str(ticker.get("s", ""))
            if symbol:
                self.tickers[symbol] = ticker
        self._refresh(self.search.text())

    def apply_theme(self, theme: dict[str, str]) -> None:
        self.theme = theme
        self.set_filter_state(
            self.filter_timeframe,
            self.minimum_volume,
            self.volume_values if self.use_volume_override else None,
        )
        self._refresh(self.search.text())

    def _refresh(self, text: str) -> None:
        selected_item = self.table.item(self.table.currentRow(), 0)
        selected_symbol = (
            str(selected_item.data(Qt.ItemDataRole.UserRole) or "")
            if selected_item is not None
            else ""
        )
        query = text.upper().strip().replace("/", "").replace("-", "").removesuffix(".P")
        query_changed = query != getattr(self, "_last_query", None)
        self._last_query = query
        if query_changed and query:
            self._query_volume_sort = True
            self.sort_mode = "volume"
        matches = [symbol for symbol in self.symbols if query in symbol]
        if query and getattr(self, "_query_volume_sort", True):
            matches.sort(key=lambda symbol: (symbol != query, -safe_float(self.tickers.get(symbol, {}).get("q")), symbol))
        elif self.sort_mode in {"symbol", "symbol_desc"}:
            matches.sort(reverse=self.sort_mode == "symbol_desc")
        elif self.sort_mode in {"price", "price_asc"}:
            matches.sort(
                key=lambda symbol: safe_float(self.tickers.get(symbol, {}).get("c")),
                reverse=self.sort_mode == "price",
            )
        elif self.sort_mode in {"hour_gainers", "hour_losers"}:
            known = [symbol for symbol in matches if symbol in self.hour_changes]
            missing = [symbol for symbol in matches if symbol not in self.hour_changes]
            known.sort(key=self.hour_changes.__getitem__, reverse=self.sort_mode == "hour_gainers")
            matches = known + missing
        elif self.sort_mode in {"gainers", "losers"}:
            matches.sort(
                key=lambda symbol: safe_float(self.tickers.get(symbol, {}).get("P")),
                reverse=self.sort_mode == "gainers",
            )
        else:
            matches.sort(
                key=self._volume_for,
                reverse=self.sort_mode != "volume_asc",
            )
        if query:
            matches.sort(key=lambda symbol: symbol != query)
        total_matches = len(matches)
        matches = matches[:120]
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(matches))
        for row, symbol in enumerate(matches):
            ticker = self.tickers.get(symbol, {})
            price = safe_float(ticker.get("c"))
            change = safe_float(ticker.get("P"))
            hour_change = self.hour_changes.get(symbol)
            volume = self._volume_for(symbol)
            values = (
                symbol,
                format_price(price) if price else "—",
                f"{hour_change:+.2f}%" if hour_change is not None else "—",
                f"{change:+.2f}%" if ticker else "—",
                human_number(volume, money=True) if volume else "—",
            )
            for column, value in enumerate(values):
                item = self.table.item(row, column)
                if item is None:
                    item = QtWidgets.QTableWidgetItem()
                    item.setFont(
                        typography_font(
                            TextRole.INSTRUMENT_SYMBOL if column == 0 else TextRole.TABLE_VALUE
                        )
                    )
                    self.table.setItem(row, column, item)
                if item.text() != value:
                    item.setText(value)
                item.setData(Qt.ItemDataRole.UserRole, symbol)
                if column > 0:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if column == 2 and hour_change is not None:
                    item.setForeground(QtGui.QColor(self.theme["green"] if hour_change >= 0 else self.theme["red"]))
                elif column == 3 and ticker:
                    item.setForeground(
                        QtGui.QColor(self.theme["green"] if change >= 0 else self.theme["red"])
                    )
                elif column == 4:
                    item.setForeground(QtGui.QColor(self.theme["muted"]))
                else:
                    item.setForeground(QtGui.QColor(self.theme["text"]))
            self.table.setRowHeight(row, 28)
        self.table.setUpdatesEnabled(True)
        if matches:
            selected_row = matches.index(selected_symbol) if not query_changed and selected_symbol in matches else 0
            self.table.setCurrentCell(selected_row, 0)
        active_filter = (
            f" · {self.filter_timeframe.upper()} VOLUME ≥ {human_number(self.minimum_volume)}"
            if self.minimum_volume > 0
            else " · NO VOLUME FILTER"
        )
        self.result_count.setText(
            f"{len(matches)} SHOWN · {total_matches} MATCHING{active_filter}"
            f" · 1H {sum(symbol in self.hour_changes for symbol in self.symbols)}/{len(self.symbols)}"
        )

    def _accept_current(self) -> None:
        row = self.table.currentRow()
        self._accept_row(row)

    def _accept_row(self, row: int) -> None:
        item = self.table.item(row, 0) if 0 <= row < self.table.rowCount() else None
        if item is None:
            return
        self.selected_symbol = str(item.data(Qt.ItemDataRole.UserRole) or item.text())
        self.accept()

    def _accept_item(self, item: QtWidgets.QTableWidgetItem) -> None:
        if item is not None:
            self._accept_row(item.row())


class MarketFilterDialog(QtWidgets.QDialog):
    def __init__(
        self,
        timeframe: str,
        minimum_volume: float,
        parent: QtWidgets.QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Market filter")
        self.setMinimumWidth(420)
        self.clear_requested = False
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        heading = QtWidgets.QLabel("FILTER BINANCE USD-M PERPETUALS")
        heading.setObjectName("dialogHeading")
        note = QtWidgets.QLabel(
            "This limits the pairs shown in Symbol Search. Sorting is controlled directly by clicking "
            "the search-table column headings."
        )
        note.setObjectName("subtleLabel")
        note.setWordWrap(True)
        form = QtWidgets.QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        self.timeframe = QtWidgets.QComboBox()
        for label, value in (("24-hour rolling volume", "24h"), ("4-hour rolling volume", "4h"), ("1-hour rolling volume", "1h")):
            self.timeframe.addItem(label, value)
        index = self.timeframe.findData(timeframe)
        self.timeframe.setCurrentIndex(max(0, index))
        self.minimum = QtWidgets.QLineEdit()
        self.minimum.setObjectName("compactAmountEdit")
        self.minimum.setPlaceholderText("No minimum · examples: 1M, 250M, 1.5B")
        self.minimum.setClearButtonEnabled(True)
        if minimum_volume > 0:
            self.minimum.setText(human_number(minimum_volume).replace("$", ""))
        self.preset_row = QtWidgets.QWidget()
        preset_layout = QtWidgets.QHBoxLayout(self.preset_row)
        preset_layout.setContentsMargins(0, 0, 0, 0)
        preset_layout.setSpacing(4)
        self.preset_buttons: dict[float, QtWidgets.QPushButton] = {}
        for label, value in (
            ("NONE", 0.0),
            ("1M", 1e6),
            ("10M", 1e7),
            ("50M", 5e7),
            ("100M", 1e8),
            ("500M", 5e8),
            ("1B", 1e9),
        ):
            button = QtWidgets.QPushButton(label)
            button.setObjectName("filterPreset")
            button.setFixedHeight(28)
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, amount=value: self._set_preset(amount))
            preset_layout.addWidget(button)
            self.preset_buttons[value] = button
        self.minimum.textEdited.connect(lambda _text: self._highlight_preset(None))
        self._highlight_preset(minimum_volume)
        form.addRow("Volume window", self.timeframe)
        form.addRow("Minimum USDT volume", self.minimum)
        form.addRow("Quick presets", self.preset_row)
        buttons = QtWidgets.QDialogButtonBox()
        clear_button = buttons.addButton("CLEAR FILTER", QtWidgets.QDialogButtonBox.ButtonRole.ResetRole)
        apply_button = buttons.addButton("APPLY", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        clear_button.clicked.connect(self._clear)
        apply_button.clicked.connect(self._accept_validated)
        buttons.rejected.connect(self.reject)
        layout.addWidget(heading)
        layout.addWidget(note)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _clear(self) -> None:
        self.clear_requested = True
        self.minimum.clear()
        self._highlight_preset(0.0)
        self.accept()

    def _set_preset(self, amount: float) -> None:
        self.clear_requested = amount <= 0
        self.minimum.setText("" if amount <= 0 else human_number(amount).replace("$", ""))
        self._highlight_preset(amount)

    def _highlight_preset(self, amount: float | None) -> None:
        for value, button in self.preset_buttons.items():
            button.setChecked(
                amount is not None
                and math.isclose(
                    value,
                    amount,
                    rel_tol=0.0,
                    abs_tol=max(1e-6, abs(value) * 1e-9),
                )
            )

    def _accept_validated(self) -> None:
        try:
            amount = parse_compact_amount(self.minimum.text())
        except ValueError:
            QtWidgets.QMessageBox.information(
                self,
                "Volume format",
                "Enter a positive amount such as 1M, 250M, 1.5B, or a full number.",
            )
            self.minimum.setFocus()
            return
        if amount < 0:
            return
        self.accept()

    def options(self) -> tuple[str, float]:
        return (
            str(self.timeframe.currentData()),
            0.0 if self.clear_requested else parse_compact_amount(self.minimum.text()),
        )
