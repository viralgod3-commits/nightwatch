Warning: truncated output (original token count: 41333)
Total output lines: 4061

"""Market statistics, metric history, watchlists, and symbol-selection widgets."""

from __future__ import annotations

import html
import math
import time
from bisect import bisect_left, insort_left
from collections import deque
from typing import Any

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt, Signal

from ..constants import (
    DEFAULT_MARKET_BAR_TIMEFRAMES, DEFAULT_SYMBOL, MARKET_SORT_MODES,
    normalized_market_bar_timeframes,
)
from ..networking.binance import launch_task
from ..utilities import (
    alpha_color,
    coin_base_symbol,
    coin_icon_bytes,
    coin_icon_exists,
    device_pixel_rect,
    format_local_time,
    line_icon,
)
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


_WATCHLIST_ICON_CACHE: dict[tuple[str, int, str, str, str, int], QtGui.QIcon] = {}


def _watchlist_row_font(role: str) -> QtGui.QFont:
    """Use a dense row face without changing the app-wide typography roles."""
    font = typography_font(role)
    pixels = font.pixelSize()
    if pixels <= 0:
        pixels = round(QtGui.QFontInfo(font).pixelSize())
    font.setPixelSize(max(1, pixels if role == TextRole.INSTRUMENT_SYMBOL else pixels - 1))
    return font


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
        return "0.00%"
    decimals = 4
    if abs(value) < 0.0001:
        decimals = min(10, max(4, 2 - math.floor(math.log10(abs(value)))))
    if round(value, decimals) == 0:
        return f"{value:+.3g}%"
    text = f"{value:+.{decimals}f}".rstrip("0")
    whole, fraction = text.split(".")
    return f"{whole}.{fraction.ljust(2, '0')}%"


def _market_funding_time(next_ms: float, event_ms: float = 0.0) -> tuple[str, str]:
    """Return a compact countdown and exact local time from the exchange schedule."""
    if not math.isfinite(next_ms) or next_ms <= 0:
        return "", "—"
    try:
        exact = format_local_time(next_ms / 1000, "%d %b %H:%M")
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
        button.setToolTip(f"Shortcut: {index + 1}")
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
    size: int = 18,
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
    cache_key = (base, dpr_key, outline, surface, ink, size)
    if cache_key in _WATCHLIST_ICON_CACHE:
        return _WATCHLIST_ICON_CACHE[cache_key]
    source = QtGui.QPixmap()
    payload = coin_icon_bytes(base)
    if payload:
        source.loadFromData(payload)
    logical_size = float(size)
    physical_size = max(size, int(math.ceil(logical_size * dpr)))
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
        painter.drawEllipse(QtCore.QRectF(1, 1, size - 2, size - 2))
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

        stamp_text = format_local_time(self.hover_stamp / 1000, "%d %b %Y · %H:%M")
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
            …21333 tokens truncated…ource.hour_changes_changed.connect(self._hour_state_changed)
        self.source.current_changed.connect(self._sync_current_symbol)
        self.source.groups_changed.connect(self._groups_changed)
        self.source.icon_changed.connect(self._icon_changed)
        typography_controller().changed.connect(self._typography_changed)
        self._groups_changed(self.source.group_snapshot())
        self.refresh()

    def _width_environment_changed(self) -> None:
        self._invalidate_numeric_widths()
        self._pending_numeric_resizes.update((2, 3, 4))
        if not self._width_environment_refresh_pending:
            self._width_environment_refresh_pending = True
            QtCore.QTimer.singleShot(0, self, self._flush_width_environment_refresh)

    def _flush_width_environment_refresh(self) -> None:
        self._width_environment_refresh_pending = False
        if self._pending_numeric_resizes:
            self.refresh()

    def _invalidate_numeric_widths(self) -> None:
        self._numeric_cell_advances.clear()
        self._numeric_column_advances.clear()
        self._numeric_text_advances.clear()
        self._numeric_font_metrics.clear()

    def _sync_numeric_column_width(self, column: int) -> None:
        """Keep Qt's exact contents sizing without rescanning unchanged widths."""
        maximum = 0.0
        for row in range(self.table.rowCount()):
            cell_key = (row, column)
            advance = self._numeric_cell_advances.get(cell_key)
            if advance is None:
                item = self.table.item(row, column)
                if item is None:
                    continue
                font = item.font()
                font_key = font.key()
                text_key = (font_key, item.text())
                advance = self._numeric_text_advances.get(text_key)
                if advance is None:
                    metrics = self._numeric_font_metrics.get(font_key)
                    if metrics is None:
                        metrics = QtGui.QFontMetricsF(font, self.table)
                        if len(self._numeric_font_metrics) >= 32:
                            self._numeric_font_metrics.clear()
                        self._numeric_font_metrics[font_key] = metrics
                    advance = metrics.horizontalAdvance(text_key[1])
                    # Live prices are unbounded; retain only a small hot set.
                    if len(self._numeric_text_advances) >= 512:
                        self._numeric_text_advances.clear()
                    self._numeric_text_advances[text_key] = advance
                self._numeric_cell_advances[cell_key] = advance
            maximum = max(maximum, advance)
        if self._numeric_column_advances.get(column) != maximum:
            # Delegate padding and header widths remain owned by Qt. Fixed-pitch
            # digit changes usually leave the maximum text advance unchanged.
            self.table.resizeColumnToContents(column)
            self._numeric_column_advances[column] = maximum

    def _typography_changed(self) -> None:
        for row in range(self.table.rowCount()):
            for column in range(1, 5):
                item = self.table.item(row, column)
                if item is not None:
                    role = TextRole.UI_BODY if column == 1 else TextRole.TABLE_VALUE
                    item.setFont(_watchlist_row_font(role))
        self._invalidate_numeric_widths()
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
            if pair_item is not None:
                pair_item.setIcon(watchlist_coin_icon(symbol, self.source.theme, size=14))
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
            self._numeric_cell_advances = {
                key: value for key, value in self._numeric_cell_advances.items()
                if key[0] < len(symbols)
            }
        now = time.monotonic()
        signatures = {}
        source_signatures = {}
        style_key = (self._theme_revision, self.devicePixelRatioF())
        if style_key != self._last_size_style_key:
            self._invalidate_numeric_widths()
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
            # Compare source values before formatting every row. Preserve the
            # sign of zero as text; it can change the displayed percentage.
            source_signature = (
                symbol, str(ticker.get("c")), str(ticker.get("P")), str(change_1h),
                bool(ticker), move_direction, symbol == active_symbol, style_key,
            )
            source_signatures[row] = source_signature
            previous = self._row_display_signatures.get(row)
            icon_due = now >= self._icon_refresh_after.get(symbol, 0.0)
            if (previous is not None and not icon_due
                    and source_signature == self._row_source_signatures.get(row)):
                signatures[row] = previous
                continue
            quote = "USDC" if symbol.endswith("USDC") else "USDT"
            base = symbol.removesuffix(quote)
            display = f"{base}{quote}.P"
            values = (
                display,
                format_price(price) if price else "—",
                f"{change_1h:+.2f}%" if change_1h is not None else "—",
                f"{change_24h:+.2f}%" if ticker else "—",
            )
            # A rounded "-0.00%" can keep its text while its color changes
            # between negative zero and a small negative value.
            change_colors = (change_1h >= 0 if change_1h is not None else None,
                             change_24h >= 0)
            signature = (symbol, values, change_colors, bool(ticker),
                         move_direction, symbol == active_symbol, style_key)
            signatures[row] = signature
            if signature == previous and not icon_due:
                continue
            static_changed = previous is None or (previous[0], previous[-1]) != (symbol, style_key)
            toggle_item = self.table.item(row, 0)
            if toggle_item is None:
                toggle_item = QtWidgets.QTableWidgetItem()
                toggle_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, 0, toggle_item)
            # Use the leading header-toggle column for row icons, leaving
            # pair text aligned with its heading and more room for the name.
            if static_changed or icon_due:
                found = coin_icon_exists(symbol)
                toggle_item.setIcon(watchlist_coin_icon(symbol, self.source.theme, size=14))
                self._icon_refresh_after[symbol] = float("inf") if found else now + 60.0
                if not found:
                    self.source.request_icon(symbol)
            toggle_item.setBackground(
                current_bg if symbol == active_symbol else QtGui.QBrush()
            )

            for column, value in enumerate(values, start=1):
                item = self.table.item(row, column)
                if item is None:
                    item = QtWidgets.QTableWidgetItem()
                    font = _watchlist_row_font(
                        TextRole.UI_BODY if column == 1 else TextRole.TABLE_VALUE
                    )
                    if column == 1 and font.pixelSize() > 0:
                        font.setPixelSize(font.pixelSize() + 1)
                    item.setFont(font)
                    self.table.setItem(row, column, item)
                if item.text() != value:
                    item.setText(value)
                    if column in (2, 3, 4):
                        dirty_columns.add(column)
                        self._numeric_cell_advances.pop((row, column), None)
                if item.toolTip():
                    item.setToolTip("")
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
                        "Unusual five-minute move"
                    )
                if column == 1:
                    item.setData(Qt.ItemDataRole.UserRole, symbol)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                    if static_changed or icon_due:
                        item.setIcon(QtGui.QIcon())
                        item.setToolTip("Right-click to remove from watchlist")
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
        self._row_source_signatures = source_signatures
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
            self._sync_numeric_column_width(column)

    def apply_theme(self, theme: dict[str, str]) -> None:
        self._theme_revision += 1
        self._invalidate_numeric_widths()
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
            matches.sort(key=lambda symbol: (symbol != query, -self._volume_for(symbol), symbol))
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
            parse_compact_amount(self.minimum.text()),
        )

